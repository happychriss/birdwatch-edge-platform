# Cloud-Check Filter — Requirements & Design

> Cross-referenced from [`requirements.md`](requirements.md) §4.2.  
> Python simulation: [`src/cloud-check/`](src/cloud-check/README.md)

---

## 1. Problem Statement

The Parallax PIR sensor responds to rapid changes in infrared radiation — not only warm moving objects but also:

- Clouds passing in front of the sun (sudden scene-wide illumination change)
- Wind moving plant leaves across the sensor's field of view
- Shadows sweeping across the balcony as the sun moves through the day

Each false trigger costs a full capture-WiFi-upload cycle (~2–3 min battery equivalent) and produces empty-scene photos that clutter the gallery and push real events off-screen.

A coarse global brightness diff was tried and disabled (see `requirements.md` §4.1): it cannot distinguish a dark bird silhouette from a cloud shadow because both darken the frame. A spatially-aware, per-region approach is required.

---

## 2. Requirements

| Priority | Requirement |
|----------|-------------|
| **Must** | Non-cloud recall = 1.0 — never suppress upload when a bird or person is present |
| **Must** | Run on ESP32-S3 within ~1.5 s after capture, before WiFi connects |
| **Must** | Self-calibrate — no manual region masks, no field threshold adjustments |
| **Should** | Filter ≥ 50 % of cloud/sun false triggers at steady state |
| **May** | Upload spurious cloud photos during the first day-period warmup phase |
| **Must not** | Use ML inference (no TFLite, no CNN weights); integer-arithmetic only for clean C port |

---

## 3. Approach

Classical per-tile anomaly detection against an adaptive per-time-bucket background model.  
**This is signal processing, not AI.** Every decision produces a human-readable rule name and reason string. No training is needed — the model is a running average that the device maintains itself.

**Why not a neural network?**  
The camera is fixed, the scene geometry is known, and the decision boundary (compact dark blob vs diffuse lighting shift) maps directly onto a handful of measurable statistics. Classical methods are:

- **Fully inspectable** — every suppressed frame shows which rule fired and why
- **Portable** — no TFLite runtime; the algorithm fits in ~2 KB of NVS + a few hundred bytes of C
- **Self-calibrating** — the background model adapts to scene changes automatically; no re-training when something on the balcony moves

---

## 4. Algorithm

### 4.1 Feature Extraction

| Parameter | Value |
|-----------|-------|
| Input | VGA (640×480) grayscale, downsampled from the OV2640 SXGA JPEG |
| Grid | 16 × 12 = 192 tiles of 40×40 px each |
| Per-tile feature | Mean intensity (uint8 equivalent) |

The 40 × 40 tile size balances spatial resolution (can localise a small bird on the railing) against robustness to JPEG compression artefacts and minor camera vibration.

### 4.2 Background Model

Per tile, per day-period bucket: **mean** (float32) and **variance** (float32), updated by EMA with α = 0.15.  
A variance floor of 36 (std = 6) prevents any tile from becoming over-confident in stable regions.

**Day-period buckets** — the day window 06:00–22:00 is split into 4 equal slots:

| Bucket | Hours | Scene character |
|--------|-------|----------------|
| 0 | 06–10 | Low eastern sun, long left-cast shadows |
| 1 | 10–14 | Near-overhead sun, short shadows |
| 2 | 14–18 | Afternoon sun, shadows extending right |
| 3 | 18–22 | Low western sun / shaded |

Coarser bucketing (4 instead of 24) means each bucket accumulates observations faster — important on a device that may trigger only a few times per hour.

**NVS footprint on device:** 4 buckets × 192 tiles × 10 bytes (mean + var + count) ≈ **7.7 KB**.

### 4.3 Decision Pipeline (in priority order)

All decisions default to **non-cloud** (upload). A frame is suppressed only when the evidence for "this is just lighting" is unambiguous.

---

#### Stage 1 — WARMUP
| | |
|---|---|
| **Condition** | Bucket has seen fewer than N frames since model reset (N=8 on device via NVS; N=0 in Python simulation — steady-state eval) |
| **Decision** | `process` — upload |
| **Model update** | Yes — every frame folds into the model (bootstrap) |

The model has too few observations to make a reliable call. Missing a bird during warmup is unacceptable; a few extra cloud uploads are not. On the device, the frame counter persists in NVS so warmup only fires on the very first boot per bucket — not on every PIR trigger. SCENE_DRIFT resets the warmup counter so the model re-bootstraps after a detected scene change.

---

#### Stage 2 — DARK_OBJ
| | |
|---|---|
| **Condition** | ≥ 1 tile satisfies **all three**: z-score > 2.5 AND tile mean dropped ≥ 35 below bucket mean AND tile mean dropped ≥ 20 below **previous frame** |
| **Decision** | `process` — upload |
| **Model update** | No |

Dark silhouettes against the bright sky or floor are the primary object cue. The **temporal check** (vs previous frame) is the critical discriminator: if those dark tiles were already present in the prior capture, the model is stale (day-boundary scene change) rather than a new arrival. Skipped on the very first frame when no previous is available.

---

#### Stage 3 — INDIRECT_LIGHT
| | |
|---|---|
| **Condition** | global_mean < 95 (but ≥ NIGHT threshold of 70) |
| **Decision** | `non-cloud` — upload |
| **Model update** | Yes |

Low-angle sun (morning/evening) creates hard directional shadows — **high spatial contrast, moderate brightness**. This is exactly when PIR false triggers are most common, and also when the background model is least reliable: the model has accumulated high variance from sun/cloud cycling in this lighting zone, so z-scores are compressed even for large object-sized deltas (a 100 DN difference yields z ≈ 2.3, below the 2.5 threshold). Cloud and non-cloud frames are indistinguishable by any spatial or luminance metric. The honest response is to upload unconditionally and let the user decide.

---

#### Stage 4 — SPOT_CHANGE
| | |
|---|---|
| **Condition** | `prev_frame` available AND `\|global_mean − prev_global_mean\|` < 10 DN AND 1–2 tiles darkened by ≥ 15 DN vs prev frame AND ≤ 20 tiles changed by ≥ 10 DN in any direction |
| **Decision** | `process` — upload |
| **Model update** | No |

Scene is globally stable (global brightness and overall tile-level churn are low) but exactly 1–2 tiles darkened noticeably since the last capture. This is the signature of a small object (distant bird, partial silhouette) that DARK_OBJ misses because its z-score vs the long-term model is below threshold. The frame-to-frame comparison is the only reliable signal. The `max_noisy_tiles` guard prevents shadow redistribution patterns (many tiles shift a little in opposite directions, global cancels out) from triggering this stage.

---

#### Stage 5 — QUIET
| | |
|---|---|
| **Condition** | ≤ 20 % of tiles are anomalous (z-score > 2.5) |
| **Decision** | `clouds` — suppress upload |
| **Model update** | Yes |

The scene is essentially identical to the stored model. Nothing happened; the PIR was triggered by a lighting change too subtle to shift more than a handful of tiles.

---

#### Stage 6 — SCENE_DRIFT
| | |
|---|---|
| **Condition** | ≥ 4 tiles are dark vs the model (same as DARK_OBJ threshold) **but** none of those tiles are newly dark vs the previous frame |
| **Decision** | `process` — upload (safety bias) |
| **Model update** | Yes — re-calibrate the stale model; warmup counter reset so model re-bootstraps |

The dark tiles were already present in the previous capture — the model is stale (items moved on the balcony, plants grew, sun angle shifted between days). Upload the frame (can't prove it's empty) and update the model so it re-calibrates within a few frames.

---

#### Default — AMBIGUOUS
| | |
|---|---|
| **Condition** | None of the above matched |
| **Decision** | `process` — upload |
| **Model update** | No |

When no rule fires with confidence, lean upload.

---

### 4.4 Stage Flow

```
frame arrives
    │
    ├─ global_mean < 70?  ───────────────────────── NIGHT           → upload + update
    │
    ├─ bucket < 8 frames seen? ──────────────────── WARMUP          → upload + update
    │
    ├─ newly dark tiles ≥ 1? ────────────────────── DARK_OBJ        → upload
    │
    ├─ global_mean < 95?  ───────────────────────── INDIRECT_LIGHT  → upload + update
    │
    ├─ global stable + 1-5 tiles dark vs prev? ─── SPOT_CHANGE     → upload
    │
    ├─ anomaly ratio ≤ 0.20? ────────────────────── QUIET           → suppress + update
    │
    ├─ dark tiles not new vs prev frame? ──────────  SCENE_DRIFT    → upload + update
    │
    └─ otherwise ───────────────────────────────── AMBIGUOUS        → upload
```

> **Note on DIFFUSE rule:** an earlier rule ("many anomalous tiles, low compactness → global lighting shift → cloud") was prototyped but removed. The grid-search ablation (`scripts/sweep.py`) showed it fires zero times on real data — every global lighting shift also produces dark tiles, so DARK_OBJ or SCENE_DRIFT intercepts it first.

---

## 5. Performance

Evaluated on 195 labelled real-scene frames, May 2026 (123 clouds, 72 process, chronological online replay).
Includes indirect-light morning sequences where clouds/process frames are luminance-indistinguishable.

| Mode | Process recall | Clouds recall | Missed birds |
|------|----------------|---------------|--------------|
| **Online (self-calibrating, no labels)** | **1.000** | **0.537** | **0** |

Parameters found by focused grid search over 5 184 configurations (`scripts/sweep.py`, QQVGA grid, 195 frames).
INDIRECT_LIGHT fires on frames with `70 ≤ global_mean < 95` — uploaded unconditionally.
SPOT_CHANGE fires on up to 2 tiles darkening vs prev while global is stable — safety net for small objects.

Key parameter history:
- `tile_z_threshold` 3.0 → 2.5: high sky-tile variance compressed z-scores below 3.0 for real objects.
- `quiet_anomaly_ratio` 0.05 → 0.20: compensates for more tiles flagged at lower z threshold.
- `night_brightness_threshold` 80 → 70: avoids model-state side effects from near-twilight frames.
- `dark_object_min_delta` 30 → 35: tighter model-delta check reduces cloud shadow false detections.
- `temporal_dark_delta` 15 → 20: tighter frame-to-frame check pairs with the above.
- `spot_change_max_tiles` 5 → 2: sweep showed cost/benefit optimum at 2 tiles (TN 63 vs 66 off).
- `scene_drift_min_tiles` 1 → 4: require bigger persistent change before SCENE_DRIFT fires.
- SCENE_DRIFT now resets warmup counter so model re-bootstraps after a scene change.

---

## 6. Training Data

`/workspace/training-data/` (images gitignored — folder structure committed):

| Folder | Count | Label | Notes |
|--------|-------|-------|-------|
| `real-data/clouds/` | ~123 | clouds | Empty balcony, varying sun/shadow, 2026-05 |
| `real-data/process-birds-pillow/` | ~42 | process | Same scene + small dark pillow as bird stand-in |
| `real-data/process-people/` | ~30 | process | Same scene + person visible |

---

## 7. Three-Project Consistency Rule

The cloud-check algorithm is implemented in **three places** that must stay in sync:

| Project | File(s) | Role |
|---------|---------|------|
| `src/cloud-check/` | `cloud_check/classifier.py`, `config.py` | Python simulation — ground truth for algorithm logic and parameter tuning |
| `src/esp_bw_src/` | `main/cloud_check.c` | ESP32-S3 C port — production firmware running on device |
| `src/python_bw_src/` | `templates/index.html`, `templates/browse_results.html` | Home server gallery — displays stage badges received from device |

**When adding or renaming a stage** (e.g. `NIGHT`, `WARMUP`, `DARK_OBJ` …) you must update all six:

1. `classifier.py` — add the new trigger string and decision logic
2. `config.py` — add any new threshold parameter with a sensible default
3. `scripts/sweep.py` — add to `ALL_STAGES` and `classify_inline()`
4. `cloud_check.c` — add the matching `#define` constant and C logic in `run_pipeline()`
5. `cloud_check.h` — update the `stage[]` field comment to list the new value
6. `serve.py` (`src/cloud-check/`) — add the stage colour to `_TRIGGER_COLOR`
7. `scripts/show_gallery.py` — add the stage colour to `TRIGGER_COLOR`
8. Both gallery templates in `src/python_bw_src/templates/` — add the stage colour `{% if stage == '...' %}`

**When changing a threshold** — update `config.py` default and the matching `#define` in `cloud_check.c`.

---

## 8. Development Path

**Phase 1 — Python simulation (complete)**  
`src/cloud-check/` — full pipeline, confusion matrix, parameter sweep, gallery server, debug inspector.

**Phase 2 — ESP-IDF C port (complete)**  
`main/cloud_check.c` — all stages ported: NIGHT, WARMUP, DARK_OBJ, INDIRECT_LIGHT, SPOT_CHANGE, QUIET, SCENE_DRIFT, AMBIGUOUS.  
Integer-arithmetic-friendly by design:

| Operation | ESP32-S3 estimate |
|-----------|-------------------|
| Tile mean extraction (192 tiles) | ~1 ms |
| EMA background update | ~0.1 ms |
| z-score computation | ~0.5 ms |
| Blob analysis (flood-fill) | ~2 ms |
| **Total** | **< 10 ms** |

Previous-frame tile means persist in RAM across the cycle (192 × 1 byte = 192 bytes). Background model persists in NVS.

**Phase 3 — Server feedback (future)**  
The home server can echo a corrective label (`cloud` / `non-cloud`) per uploaded image to accelerate model re-calibration after scene changes, without requiring any firmware update.  
Stage badges received from the device are already displayed in the gallery (`python_bw_src`).

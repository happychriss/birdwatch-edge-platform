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
| **Condition** | Bucket has seen fewer than 8 frames since model reset |
| **Decision** | `non-cloud` — upload |
| **Model update** | Yes — every frame folds into the model (bootstrap) |

The model has too few observations to make a reliable call. Missing a bird during warmup is unacceptable; a few extra cloud uploads are not.

---

#### Stage 2 — DARK_OBJ
| | |
|---|---|
| **Condition** | ≥ 1 tile satisfies **all three**: z-score > 3.0 AND tile mean dropped ≥ 30 below bucket mean AND tile mean dropped ≥ 15 below **previous frame** |
| **Decision** | `non-cloud` — upload |
| **Model update** | No |

Dark silhouettes against the bright sky or floor are the primary object cue. The **temporal check** (vs previous frame) is the critical discriminator: if those dark tiles were already present in the prior capture, the model is stale (day-boundary scene change) rather than a new arrival. Skipped on the very first frame when no previous is available.

---

#### Stage 3 — QUIET
| | |
|---|---|
| **Condition** | ≤ 5 % of tiles are anomalous (z-score > 3.0) |
| **Decision** | `cloud` — suppress upload |
| **Model update** | Yes |

The scene is essentially identical to the stored model. Nothing happened; the PIR was triggered by a lighting change too subtle to shift more than a handful of tiles.

---

#### Stage 4 — SCENE_DRIFT
| | |
|---|---|
| **Condition** | ≥ 1 tile is dark vs the model (same as DARK_OBJ threshold) **but** none of those tiles are newly dark vs the previous frame |
| **Decision** | `non-cloud` — upload (safety bias) |
| **Model update** | Yes — re-calibrate the stale model |

The dark tiles were already present in the previous capture — the model is stale (items moved on the balcony, plants grew, sun angle shifted between days). Upload the frame (can't prove it's empty) and update the model so it re-calibrates within a few frames.

---

#### Default — AMBIGUOUS
| | |
|---|---|
| **Condition** | None of the above matched |
| **Decision** | `non-cloud` — upload |
| **Model update** | No |

When no rule fires with confidence, lean upload.

---

### 4.4 Stage Flow

```
frame arrives
    │
    ├─ bucket < 8 frames seen? ──────────────────── WARMUP      → upload + update
    │
    ├─ newly dark tiles ≥ 1? ────────────────────── DARK_OBJ    → upload
    │
    ├─ anomaly ratio ≤ 0.05? ────────────────────── QUIET       → suppress + update
    │
    ├─ dark tiles not new vs prev frame? ──────────  SCENE_DRIFT → upload + update
    │
    └─ otherwise ───────────────────────────────── AMBIGUOUS    → upload
```

> **Note on DIFFUSE rule:** an earlier rule ("many anomalous tiles, low compactness → global lighting shift → cloud") was prototyped but removed. The grid-search ablation (`scripts/sweep.py`) showed it fires zero times on real data — every global lighting shift also produces dark tiles, so DARK_OBJ or SCENE_DRIFT intercepts it first.

---

## 5. Performance

Evaluated on 147 labelled real-scene frames, May 2026 (109 cloud, 38 non-cloud, chronological online replay).

| Mode | Non-cloud recall | Cloud recall | Missed birds |
|------|-----------------|--------------|--------------|
| Oracle (ground-truth model updates) | 1.000 | 0.688 | 0 |
| **Online (self-calibrating, no labels)** | **1.000** | **0.606** | **0** |

Parameters were found by exhaustive grid search over 18 144 configurations (`scripts/sweep.py`): 6 282 configs achieve zero missed birds; the production config maximises cloud filtering among those.

The oracle-vs-online gap in cloud recall is caused by day-boundary scene changes (items moved on balcony overnight) where SCENE_DRIFT must re-calibrate over several frames before QUIET starts firing reliably.

---

## 6. Training Data

`/workspace/training-data/` (images gitignored — folder structure committed):

| Folder | Count | Label | Notes |
|--------|-------|-------|-------|
| `real-data/sun/` | 109 | cloud | Empty balcony, varying sun/shadow, 2026-05 |
| `real-data/birds-simu/` | 11 | non-cloud | Same scene + small dark pillow as stand-in |
| `real-data/people/` | 27 | non-cloud | Same scene + person visible |
| `with-birds/` | 31 | non-cloud (aux, held out) | 2025-07 scene, different angle, cross-domain check |

---

## 7. Development Path

**Phase 1 — Python simulation (complete)**  
`src/cloud-check/` — full pipeline, confusion matrix, parameter sweep, gallery server, debug inspector.

**Phase 2 — ESP-IDF C port (planned)**  
Re-implement in `cloud_check.c`. The pipeline is integer-arithmetic-friendly by design:

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

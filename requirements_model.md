# BirdWatch — Cloud-Detection Algorithm

PIR sensors are sensitive to rapid infrared changes caused by moving shadows or cloud cover. Two complementary filters are applied in sequence, both implemented on-device (C) and mirrored in Python for server-side validation.

---

## 0. Problem Statement & Design Rationale

The Parallax PIR sensor responds to rapid changes in infrared radiation — not only warm moving objects but also:

- Clouds passing in front of the sun (sudden scene-wide illumination change)
- Wind moving plant leaves across the sensor's field of view
- Shadows sweeping across the balcony as the sun moves through the day

Each false trigger costs a full capture-WiFi-upload cycle (~2–3 min battery equivalent) and produces empty-scene photos that clutter the gallery and push real events off-screen.

A coarse global brightness diff was tried and disabled: it cannot distinguish a dark bird silhouette from a cloud shadow because both darken the frame. A spatially-aware, per-region approach is required.

### 0.1 Requirements

| Priority | Requirement |
|----------|-------------|
| **Must** | Non-cloud recall = 1.0 — never suppress upload when a bird or person is present |
| **Must** | Run on ESP32-S3 within ~1.5 s after capture, before WiFi connects |
| **Must** | Self-calibrate — no manual region masks, no field threshold adjustments |
| **Should** | Filter ≥ 50 % of cloud/sun false triggers at steady state |
| **May** | Upload spurious cloud photos during the first day-period warmup phase |
| **Must not** | Use ML inference (no TFLite, no CNN weights); integer-arithmetic only for clean C port |

### 0.2 Approach

Classical per-tile anomaly detection against an adaptive per-time-bucket background model. **This is signal processing, not AI.** Every decision produces a human-readable rule name and reason string. No training is needed — the model is a running average that the device maintains itself.

**Why not a neural network?** The camera is fixed, the scene geometry is known, and the decision boundary (compact dark blob vs diffuse lighting shift) maps directly onto a handful of measurable statistics. Classical methods are:

- **Fully inspectable** — every suppressed frame shows which rule fired and why
- **Portable** — no TFLite runtime; the algorithm fits in ~2 KB of NVS + a few hundred bytes of C
- **Self-calibrating** — the background model adapts to scene changes automatically; no re-training when something on the balcony moves

---

## Three-Project Consistency Rule

Any change to this algorithm must be kept in sync across:
- `src/esp_bw_src/` — ESP32-S3 firmware (C)
- `src/cloud-check/` — Python algorithm package + parity validator
- `src/python_bw_src/` — Flask web server + display spec

**When adding or renaming a stage** (e.g. `NIGHT`, `WARMUP`, `DARK_OBJ` …) you must update all of the following:

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

## 1. Burst-Mode Sequence Filter

> **Python reference:** `src/cloud-check/cloud_check/burst_filter.py`  
> **Evaluation:** `src/cloud-check/validate_burst.py`  
> **ESP firmware:** `cloud_check.c` (`CC_BURST_*` `#define` constants)  
> **Status:** Implemented in ESP firmware and Python server. Updated with chroma-aware DUPLICATE 2026-05-25.

Runs first. Compares each PIR event's JPEG-decoded tile means directly to the **previous captured frame** (stored in NVS as `cc_p` / `cc_pu` / `cc_pv` / `cc_pgm`). Suppresses PIR re-fires on the same sun/cloud transition before the background model runs.

### 1.1 Decision Pipeline

| Stage | Condition | Decision |
|-------|-----------|----------|
| FIRST | no previous frame in NVS | process |
| BRIGHTNESS_SHIFT | \|gm_diff\| > 12 DN | process (whole-scene shift — bird could coincide) |
| DUPLICATE | n_changed == 0 **AND** n_chroma == 0 | suppress (pixel- and chroma-identical re-fire) |
| BRIGHT_STABLE | gm > 160 AND n_dark < 35 | suppress (bright scene, no shadow-casting object) |
| DIFFUSE | n_dark ≥ 60 tiles | suppress (cloud shadow sweeping entire scene) |
| SAFE | default | process (safety bias) |

Note: FAST_SHIFT and ISOLATED stages (present in `burst_filter.py`) require `dt_seconds` which is unavailable on-device before WiFi/SNTP — omitted from firmware; validated offline via `validate_burst.py`.

The chroma DUPLICATE rule fixes the frame 517/518 pigeon suppression: a pigeon produces ΔC² ≈ 27 (ΔC ≈ 5.2) on its tile, well above the 64-threshold gate, so it no longer fires DUPLICATE even when Y saturates at noon.

### 1.2 NVS State

| Key | Type | Contents |
|-----|------|----------|
| `cc_p` | uint8 array (300 B) | Y tile means of previous frame (20×15 grid) |
| `cc_pu` | uint8 array (300 B) | U tile means of previous frame |
| `cc_pv` | uint8 array (300 B) | V tile means of previous frame |
| `cc_pgm` | uint8 | Global mean (Y) of previous frame |

### 1.3 Telemetry Fields

| Field | Type | Description |
|-------|------|-------------|
| `burst_trigger` | bool | True if burst filter fired (suppressed or passed) |
| `burst_label` | string | Stage name that fired |
| `burst_gm_diff` | int | \|current gm − previous gm\| |
| `burst_n_changed` | int | Tiles where \|Y_current − Y_prev\| > 12 DN |
| `burst_n_dark` | int | Tiles darker by > 12 DN vs prev frame |
| `burst_n_chroma` | int | Tiles where ΔU² + ΔV² > 64 vs prev frame |

### 1.4 Validation Results (2026-05-23, 224 sun frames, 90 process frames)

| Category | Suppressed | Errors |
|----------|-----------|--------|
| Sun (target) | 103/224 (46%) | 0 |
| Birds/pillow | 0/44 | 0 ✓ |
| People | 5/46 | acceptable (large body shadows exceed diffuse threshold) |

---

## 2. Background-Model Pipeline

> **Python simulation:** `src/cloud-check/`  
> **Python classes:** `classifier.py`, `config.py`, `features.py`  
> **ESP firmware:** `cloud_check.c`

Runs after the burst filter passes. Per-tile EMA background model with z-score anomaly detection on a 20×15 tile grid (300 tiles). Input tile means come from on-device JPEG decode (TJpgDec ROM, streaming BT.601 YCbCr accumulation — no raw YUV422 capture).

**Model update policy:** only frames with `source == "rtc"` (15-min RTC wakeup cycle) update the background model. PIR-triggered frames are evidence-only — they contribute to the decision but never mutate the model state.

**Photo-bucket × scene-bucket structure.** The background model is indexed by two dimensions:
- **`photo_bucket`** (outer, 3 values): `NORMAL` (80 ≤ gm < 160), `BRIGHT` (gm ≥ 160), `LOWLIGHT` (gm < 80) — derived from the LIGHTCHECK metering shot.
- **`scene_bucket`** (inner, K=1): always 0 for now; signature is forward-compatible for K > 1.

### 2.1 Decision Pipeline

| Stage | Condition | Decision |
|-------|-----------|----------|
| WARMUP | model count < 4 real updates | process |
| DARK_BLOB | dark_tiles ≥ 1 AND dark_blob_max ≥ 1 AND dark_blob_max ≤ 5 | process |
| QUIET | ratio ≤ 0.25 | suppress |
| AMBIGUOUS | default | process |

**DARK_BLOB** replaces the old DARK_OBJ. A tile qualifies as "dark" if Y ≥ 20 DN below the model mean, OR chroma shift ΔC² > 64 vs model. Connected components of dark tiles are computed 8-connected on the 20×15 grid. A blob of 1–5 tiles is bird-sized; blobs larger than 5 are cloud shadows or scene drift and fall through to AMBIGUOUS.

**NIGHT** (gm < 70) was moved to Layer-1 (burst filter) as a raw sensor property — no model math needed. It sets `skip_bg_model=True` so the background model never runs.

**SCENE_DRIFT** removed. Diffuse model drift produces large blobs (> 5 tiles) which never qualify as DARK_BLOB and reach AMBIGUOUS naturally. The model side-effect (force update + warmup reset) was a workaround for the old `new_dark_tiles` condition and is no longer needed.

### 2.2 Key Parameters

| Parameter | Value | Location |
|-----------|-------|----------|
| Grid size | 20×15 (300 tiles, 8×8 px each) | `CC_TILES_X/Y` in `cloud_check.c`; `GRID_W/H` in `features.py` |
| Z-score threshold | 3.0 | `cloud_check.c`; `config.py` |
| Quiet ratio threshold | 0.25 | `cloud_check.c`; `config.py` |
| Dark tile model delta | ≥ 20 DN below model mean | `dark_object_min_delta` in `config.py` |
| Dark blob max size | ≤ 5 tiles (8-connected) | `dark_blob_max_size` in `config.py` |
| Dark blob min tiles | ≥ 1 | `dark_object_min_tiles` in `config.py` |
| Chroma DUPLICATE gate | ΔC² > 64 (ΔC > 8 linear) | `BW_CC_CHROMA_DELTA_THR_SQ` in `config.h` |
| Chroma DARK_BLOB gate | ΔC² > 64 vs model mean | `chroma_dark_obj_gate_sq` in `config.py` |
| Photo-bucket BRIGHT threshold | gm ≥ 160 DN | `BW_BRIGHT_PHOTO_THRESHOLD` in `config.h` |
| Photo-bucket LOWLIGHT threshold | gm < 80 DN | `BW_LOWLIGHT_PHOTO_THRESHOLD` in `config.h` |
| NIGHT threshold (Layer-1) | gm < 70 DN | `night_brightness_threshold` in `BurstConfig` |
| Warmup frames | 4 | `warmup_frames_per_bucket` in `config.py` |

### 2.3 Telemetry Fields

| Field | ESP key | Python field | Description |
|-------|---------|--------------|-------------|
| `source` | `source` | `source` | Wakeup source: `"pir"` or `"rtc"` |
| `photo_bucket` | `photo_bucket` | `photo_bucket` | Exposure regime: `"NORMAL"`, `"BRIGHT"`, `"LOWLIGHT"` |
| `scene_bucket` | `scene_bucket` | `scene_bucket` | Shadow-pattern cluster index (always `0`, K=1) |
| `global_mean` | `gm` | `global_mean` | Mean Y over all 300 tile means |
| `dark_tiles` | `dark_t` | `dark_tiles` | Tiles ≥ 20 DN below model mean OR chroma-shifted (ΔC² > 64). Shown blue in overlay. |
| `dark_blob_max` | `dark_blob_max` | `dark_blob_max` | Largest 8-connected cluster of dark_tiles. 1–5 → DARK_BLOB. Shown red in overlay. |
| `n_chroma_changed` | `n_chroma_changed` | `n_chroma_changed` | Tiles where ΔC² > 64 vs model mean |
| `ratio` | `ratio` | `ratio` | z-anomalous dark tile fraction (used for QUIET gate) |
| `stage` | `stage` | `stage` | Stage name that fired |
| `result` | `result` | `result` | `"process"` or `"clouds"` |
| `tile_means` | `tile_means` | `tile_means` | 300-element Y tile means (from JPG decode in backfill) |
| `tile_means_u` | `tile_means_u` | `tile_means_u` | 300-element U tile means (BT.601, centred at 128) |
| `tile_means_v` | `tile_means_v` | `tile_means_v` | 300-element V tile means |
| `model_tile_means` | `model_tile_means` | `model_tile_means` | 300-element Y background model snapshot (pre-update) |
| `tile_color_mask` | — | `tile_color_mask` | 300-element overlay: 0=none, 1=blue (dark_tile), 2=red (dark_blob) |
| `tile_delta_luma` | — | `tile_delta_luma` | 300-element Δluma = model_y − tile_y (positive = darker than model) |
| `tile_delta_chroma` | — | `tile_delta_chroma` | 300-element Δchroma = √(ΔU²+ΔV²) vs model |

### 2.4 Validation Results (342 frames id≥511, online self-calibrating, HP pipeline)

| Metric | Value |
|--------|-------|
| Bird recall (labelled frames) | **1.000** — 5/5, zero misses (incl. 3 previously missed) |
| Suppression (non-bird, non-ignore, excl. DUPLICATE) | **~18%** |

Note: `model_tile_means` in the DB currently holds the HP-adjusted model
(written by `backfill_hp.py`). The raw overlay Δm therefore shows HP delta,
not raw model delta. `result_prev`/`stage_prev` preserve the pre-HP values.

---

## 3. Full Pipeline Reference Table

Complete decision pipeline in execution order. Layer-1 (burst pre-filter, steps 1–7) compares to previous frame. Layer-2 (background model, steps 8–11) only runs when Layer-1 passes without suppression and `skip_bg_model` is false.

| # | Stage | Layer | Condition | Values / thresholds | Variable definitions | `result` | `stage` |
|---|---|---|---|---|---|---|---|
| 1 | **FIRST** | L1 | no previous frame in NVS | — | — | process | FIRST |
| 2 | **BRIGHTNESS_SHIFT** | L1 | `\|gm_diff\|` > 12 DN | > 12 → fires | `gm_diff`: \|current gm − previous gm\| | process | BRIGHTNESS_SHIFT |
| 3 | **DUPLICATE** | L1 | `n_changed` == 0 **AND** `n_chroma` == 0 | both zero | `n_changed`: tiles \|Y_cur−Y_prev\| > 12 DN; `n_chroma`: ΔU²+ΔV² > 64 vs prev | clouds | DUPLICATE |
| 4 | **BRIGHT_STABLE** | L1 | `gm` > 160 **and** `n_dark` < 35 | gm > 160; n_dark 0–34 | `n_dark`: tiles darker > 12 DN vs prev | clouds | BRIGHT_STABLE |
| 5 | **NIGHT** | L1 | `gm` < 70 | 0–69 DN | raw sensor property; sets `skip_bg_model=True` | process | NIGHT |
| 6 | **DIFFUSE** | L1 | `n_dark` ≥ 60 | ≥ 60/300 tiles | `n_dark`: same as above | clouds | DIFFUSE |
| 7 | **SAFE** | L1 | default | — | — | → L2 | SAFE |
| 8 | **WARMUP** | L2 | model `count` < 4 real updates | 0–3 updates | `count`: actual EMA update count per bucket cell | process | WARMUP |
| 9 | **DARK_BLOB** | L2 | `dark_tiles` ≥ 1 **and** `dark_blob_max` ≤ 5 | blob 1–5 tiles | `dark_tiles`: tiles ≥ 20 DN below model OR ΔC²>64 vs model; `dark_blob_max`: largest 8-connected cluster | process | DARK_BLOB |
| 10 | **QUIET** | L2 | `ratio` ≤ 0.25 | ≤ 75/300 tiles | `ratio`: z-anomalous dark tile fraction | clouds | QUIET |
| 11 | **AMBIGUOUS** | L2 | default | — | — | process | AMBIGUOUS |

---

## 4. Backfill & Calibration

### 4.1 Backfill pipeline (`backfill_meta.py`)

**JPG is the single source of truth.** Backfill always decodes tile features from the raw JPEG — it never uses `tile_means`/`tile_means_u` stored in DB meta. This guarantees reproducibility: running backfill twice on the same dataset produces identical results.

JPG source priority: local folder (`JPG_FOLDER_PATH` in `.env`) → HTTP fetch from photo server.

**Two-pass seed workflow** (run from dev container, not production):

```bash
cd src/cloud-check
source /workspace/src/python_bw_src/.venv/bin/activate

# Pass 1 — frames 604+ → let EMA converge → save model snapshot
python backfill_meta.py --from-frame 604 --save-seed model_seed.json \
  --photo-server http://192.168.1.110:8000

# Pass 2 — all frames with pre-trained seed → gallery updated
python backfill_meta.py --load-seed model_seed.json \
  --photo-server http://192.168.1.110:8000
```

`model_seed.json` format: plain JSON with per-photo-bucket per-tile float arrays (mean_y/u/v). Lives in `src/cloud-check/`, gitignored. Future use: seed the ESP32 NVS model via WiFi.

**Why two passes?** Starting from flat 128 (or a rough corpus average) the EMA takes many RTC frames to converge. Pass 1 processes frames 604+ where the model has enough data to be well-trained, then snapshots the converged state. Pass 2 replays all frames from the beginning using that state — so even early frames are classified against a realistic model.

### 4.2 Validator (`validate.py`)

Replays stored ESP telemetry against the Python model and checks parity. Driven by `validate_config.json`. `display_spec.py` controls field rendering in the gallery.

- **dt-based stages** (FAST_SHIFT, ISOLATED): skipped in validator — unavailable on-device before WiFi sync.
- **RTC-gated updates:** only `source == "rtc"` frames update the background model. PIR frames run the decision pipeline but never mutate model state.
- **Two metadata sources:** (1) ESP firmware emits telemetry on upload → stored in `meta`; (2) `backfill_meta.py` recomputes from JPEGs → `simulated=True`. `validate.py` checks parity.

---

## 5. Performance History

Background-model pipeline evaluated on labelled real-scene frames (online self-calibrating, no labels used during inference).

| Metric | Value |
|--------|-------|
| Non-cloud recall (birds/people) | **1.000** — zero misses |
| Cloud recall (false-trigger suppression) | **0.606** |

Key parameter history (background-model pipeline):
- `tile_z_threshold` 3.0 → 2.5 → **3.0**: reverted — 2.5 caused high sky-tile z-score inflation.
- `quiet_anomaly_ratio` 0.05 → 0.20 → **0.25**: production value.
- `night_brightness_threshold` 80 → **70**: avoids near-twilight model side-effects.
- `dark_object_min_delta` 30 → **35** → **20** (HP pipeline): HP removes illumination gradient so lower threshold viable.
- SCENE_DRIFT removed: large blobs (>5 tiles) naturally reach AMBIGUOUS in HP space.
- Grid upgraded from 16×12 to **20×15 (8×8 px tiles, QQVGA)**.
- **2026-05-29**: DARK_OBJ replaced by DARK_BLOB (remove `new_dark_tiles` gate); blob upper-cap removed in HP pipeline.
- **2026-05-29**: Affine illumination normalization replaced by spatial high-pass (box-blur k=5) in experimental Python pipeline (`backfill_hp.py`). ESP32 firmware still runs DARK_BLOB (no affine, no HP).

---

## 6. Training Data

`/workspace/training-data/` (images gitignored — folder structure committed):

| Folder | Label | Notes |
|--------|-------|-------|
| `ignore-sun_shining/` | suppress | Noon sun false-triggers; burst filter target (224 frames) |
| `process-birds-pillow/` | process | Toy bird + pillow as proxy objects (26 frames) |
| `process-real-birds/` | process | Real bird captures 2026-05-21 (18 frames) |
| `process-people/` | process | Person legs/body in frame (46 frames) |
| `process-dark/` | process | Reserved for night/low-light captures |
| `duplicates/` | — | Byte-identical PIR triplets moved here; originals preserved (36 frames) |

All images are SXGA JPEG (from server), downsampled to 160×120 QQVGA grayscale for on-device processing and burst filter evaluation.

---

## 7. Development Path

**Phase 1 — Python simulation (complete)**
`src/cloud-check/` — full pipeline, confusion matrix, parameter sweep (`scripts/sweep.py`, 5 184 configurations), gallery server (`serve.py`), debug inspector.

**Phase 2 — ESP-IDF C port (complete)**
`main/cloud_check.c` — all stages ported: NIGHT, WARMUP, DARK_OBJ, QUIET, SCENE_DRIFT, AMBIGUOUS (background model) plus FIRST, BRIGHTNESS_SHIFT, DUPLICATE, BRIGHT_STABLE, DIFFUSE, SAFE (burst filter).

**Phase 3 — Chroma-aware pipeline + photo-bucket model (complete 2026-05-25)**

Root cause: frames 517 (RTC) and 518 (PIR, pigeon visible) were both suppressed as DUPLICATE because the LIGHTCHECK sensor profile saturated at noon (aec_value=500, gainceiling=32×), making tile_means pixel-identical. The pigeon tile shows ΔC ≈ 5.2 (ΔC² ≈ 27) in BT.601 YCbCr — chroma cleanly separates it from cloud/sky flicker.

Changes landed across five PRs:
- **PR-1** Python algorithm: `BackgroundModel` shape `(3,1,15,20)` (photo-bucket × scene-bucket × grid); chroma DUPLICATE and DARK_OBJ gate; RTC-gated `model.update()`.
- **PR-2** Python tooling: `backfill_meta.py` full rewrite (YUV from PIL YCbCr, photo-bucket replay, RTC-gated); `validate.py` updated.
- **PR-3** Flask server: RTC-only live model, `photo_bucket` badge, `source` badge, filter checkboxes, always-tiles toggle.
- **PR-4** ESP firmware: two-phase camera pipeline (LIGHTCHECK metering → photo_bucket → JPEG capture → TJpgDec on-device decode); 3-photo-bucket × 1-scene-bucket NVS model; chroma DUPLICATE; DARK_OBJ chroma gate; `bw_cc_set_source()` RTC gate.
- **Backfill**: 556 historical frames reprocessed with `backfill_meta.py`.

On-device performance (ESP32-S3, SXGA JPEG path):

| Operation | Estimate |
|-----------|----------|
| LIGHTCHECK metering shot (init + capture) | ~530 ms |
| JPEG capture (SXGA, photo-bucket profile) | ~530 ms |
| TJpgDec on-device decode to YUV tile means | ~300–400 ms |
| Cloud-check pipeline (z-scores, burst, model update) | ~10 ms |
| **Total compute (excl. WiFi)** | **~1.4–1.5 s** |

NVS key scheme (per photo-bucket, suffix `_n`/`_b`/`_l` for NORMAL/BRIGHT/LOWLIGHT):

| Keys | Contents |
|------|----------|
| `cc_my_n/b/l`, `cc_my_n/b/l` | Y mean per photo-bucket (300 × float32 each) |
| `cc_mu_n/b/l`, `cc_mv_n/b/l` | U/V mean per photo-bucket |
| `cc_vy_n/b/l`, `cc_vu_n/b/l`, `cc_vv_n/b/l` | Y/U/V variance |
| `cc_s_n/b/l` | frames_seen counter per photo-bucket |
| `cc_p`, `cc_pu`, `cc_pv`, `cc_pgm` | Previous frame Y/U/V tile means + global mean |

Legacy keys (`cc_m0..3`, `cc_v0..3`, `cc_s0..3`) are erased on first flash after upgrade.

**Phase 4 — Pipeline refactor (complete Python 2026-05-29, ESP32 pending)**
- Python: DARK_BLOB replaces DARK_OBJ (no `new_dark_tiles` gate); NIGHT → Layer-1; SCENE_DRIFT removed; affine illumination fit added; single photo-bucket (num_photo_buckets=1).
- ESP32: still runs DARK_OBJ + SCENE_DRIFT + 3 photo-buckets. **Pending: port DARK_BLOB to C** (see `cloud_check.c` TODO).

**Phase 5 — HP pipeline experiment (Python only, 2026-05-29)**
`backfill_blur.py` and `backfill_hp.py` implement a spatial high-pass (box-blur) Layer-2
that strips illumination gradients before anomaly detection. Recovers 3 previously-missed
bird frames. Suppression ceiling ~22% on current 5-bird dataset.
Key finding: suppression improvement blocked by lack of labelled bird data, not by algorithm.
Target: 20+ labelled bird frames before re-tuning thresholds.

**Phase 6 — ESP32 DARK_BLOB port (complete 2026-05-29)**
See `cloud_check.c` for connected-component blob detection replacing `new_dark_tiles`.

**Phase 7 — Server feedback (future)**
The home server can echo a corrective label per uploaded image to accelerate model
re-calibration after scene changes, without requiring a firmware update.

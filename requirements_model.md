# BirdWatch — Cloud-Detection Algorithm

PIR sensors are sensitive to rapid infrared changes caused by moving shadows or cloud cover. Two complementary filters are applied in sequence, both implemented on-device (C) and mirrored in Python for server-side validation.

**Reference documents:**
- [`model.md`](model.md) — full detection model design, thresholds, lighting buckets, blob check, performance
- [`architecture.md`](architecture.md) — system setup, database, server, dev workflow, labeling conventions

**Three-project consistency rule:** Any change to this algorithm must be kept in sync across:
- `src/esp_bw_src/` — ESP32-S3 firmware (C)
- `src/cloud-check/` — Python algorithm package + parity validator
- `src/python_bw_src/` — Flask web server + display spec

---

## 1. Burst-Mode Sequence Filter

> **Python reference:** `src/cloud-check/cloud_check/burst_filter.py`  
> **Evaluation:** `src/cloud-check/validate_burst.py`  
> **ESP firmware:** `cloud_check.c` (`CC_BURST_*` `#define` constants)  
> **Status:** Implemented in ESP firmware and Python server. Validated 2026-05-23.

Runs first. Compares each PIR event's QQVGA frame directly to the **previous captured frame** (stored in NVS as `cc_p` / `cc_pgm`). Suppresses PIR re-fires on the same sun/cloud transition before the background model runs.

### 1.1 Decision Pipeline

| Stage | Condition | Decision |
|-------|-----------|----------|
| FIRST | no previous frame in NVS | process |
| BRIGHTNESS_SHIFT | \|gm_diff\| > 12 DN | process (whole-scene shift — bird could coincide) |
| DUPLICATE | n_changed ≤ 0 tiles | suppress (pixel-identical re-fire) |
| BRIGHT_STABLE | gm > 160 AND n_dark < 35 | suppress (bright scene, no shadow-casting object) |
| DIFFUSE | n_dark ≥ 60 tiles | suppress (cloud shadow sweeping entire scene) |
| SAFE | default | process (safety bias) |

Note: FAST_SHIFT and ISOLATED stages (present in `burst_filter.py`) require `dt_seconds` which is unavailable on-device before WiFi/SNTP — omitted from firmware; validated offline via `validate_burst.py`.

### 1.2 NVS State

| Key | Type | Contents |
|-----|------|----------|
| `cc_p` | uint8 array | Tile means of previous frame (300 tiles = 20×15 grid) |
| `cc_pgm` | uint8 | Global mean of previous frame |

### 1.3 Telemetry Fields

| Field | Type | Description |
|-------|------|-------------|
| `burst_trigger` | bool | True if burst filter fired (suppressed or passed) |
| `burst_label` | string | Stage name that fired |
| `burst_gm_diff` | int | \|current gm − previous gm\| |
| `burst_n_changed` | int | Tiles where \|current − prev\| > 12 DN |
| `burst_n_dark` | int | Tiles darker by > 12 DN vs prev frame |

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

Runs after the burst filter passes. Per-tile EMA background model with z-score anomaly detection on a 20×15 tile grid (160×120 QQVGA = 8×8 px per tile, 300 tiles total).

### 2.1 Decision Pipeline

| Stage | Condition | Decision |
|-------|-----------|----------|
| NIGHT | global_mean < 70 DN | process |
| WARMUP | frames_seen < 4 | process |
| DARK_OBJ | dark_tiles ≥ 1 AND new_dark_tiles ≥ 1 | process |
| QUIET | ratio ≤ 0.25 | suppress |
| SCENE_DRIFT | dark_tiles ≥ 4 AND new_dark_tiles == 0 | process + re-calibrate |
| AMBIGUOUS | default | process |

### 2.2 Key Parameters

| Parameter | Value | Location |
|-----------|-------|----------|
| Grid size | 20×15 (300 tiles, 8×8 px each) | `CC_TILES_X/Y` in `cloud_check.c`; `GRID_W/H` in `features.py` |
| Z-score threshold | 3.0 | `cloud_check.c`; `config.py` |
| Quiet ratio threshold | 0.25 | `cloud_check.c`; `config.py` |
| DARK_OBJ model delta | ≥ 35 DN below model mean | `cloud_check.c` |
| DARK_OBJ prev delta | ≥ 20 DN below previous frame | `cloud_check.c` |
| SCENE_DRIFT dark_tiles min | 4 | `cloud_check.c` |
| Warmup frames | 4 | `cloud_check.c` |
| Night threshold | 70 DN | `cloud_check.c` |

### 2.3 Telemetry Fields

| Field | ESP key | Python field | Description |
|-------|---------|--------------|-------------|
| `global_mean` | `gm` | `global_mean` | Mean of all 300 tile means |
| `frames_seen` | `frames_seen` | `frames_seen` | Non-NIGHT frames processed since last reset |
| `dark_tiles` | `dark_t` | `dark_tiles` | Tiles with z > 3.0 AND ≥ 35 DN below model |
| `new_dark_tiles` | `new_dark_t` | `new_dark_tiles` | Tiles with z > 3.0 AND ≥ 20 DN below prev frame |
| `ratio` | `ratio` | `ratio` | dark_tiles / 300 |
| `stage` | `stage` | `stage` | Stage name that fired |
| `result` | `result` | `result` | `"process"` or `"clouds"` |

### 2.4 Validation Results (147 labelled frames, online self-calibrating)

| Metric | Value |
|--------|-------|
| Non-cloud recall (birds/people) | **1.000** — zero misses |
| Cloud recall (false-trigger suppression) | **0.606** |

---

## 3. Full Pipeline Reference Table

Complete decision pipeline in execution order. Steps 1–6 are the burst pre-filter (compares to previous frame); steps 7–12 are the background-model pipeline, only reached when step 6 passes.

| # | Stage | Condition | Values / thresholds | Variable definitions | `result` | `stage` |
|---|---|---|---|---|---|---|
| 1 | **FIRST** | no previous frame in NVS | — | — | process | FIRST |
| 2 | **BRIGHTNESS_SHIFT** | `\|gm_diff\|` > 12 DN | 0–12 → continue; **> 12 → fires** | `gm_diff`: \|current frame mean − previous frame mean\| | process | BRIGHTNESS_SHIFT |
| 3 | **DUPLICATE** | `n_changed` == 0 | 0 → fires | `n_changed`: tiles where \|current − prev\| > 12 DN (any direction) | clouds | DUPLICATE |
| 4 | **BRIGHT_STABLE** | `gm` > 160 **and** `n_dark` < 35 | gm > 160 DN; n_dark 0–34 | `gm`: mean brightness of current frame (mean of 300 tiles); `n_dark`: tiles that got *darker* by > 12 DN vs prev | clouds | BRIGHT_STABLE |
| 5 | **DIFFUSE** | `n_dark` ≥ 60 | ≥ 60/300 tiles | `n_dark`: same as above | clouds | DIFFUSE |
| 6 | **SAFE** | default burst pass | n_dark 1–59, or gm ≤ 160 | — | → bg model | SAFE |
| 7 | **NIGHT** | `global_mean` < 70 | 0–69 DN | `global_mean`: mean of all 300 tile means (= `gm`) | process | NIGHT |
| 8 | **WARMUP** | `frames_seen` < 4 | 0–3 | `frames_seen`: non-NIGHT frames processed since last flash/reset | process | WARMUP |
| 9 | **DARK_OBJ** | `dark_tiles` ≥ 1 **and** `new_dark_tiles` ≥ 1 | both ≥ 1 | `dark_tiles`: tiles with z > 3.0 AND ≥ 35 DN below model mean; `new_dark_tiles`: tiles with z > 3.0 AND ≥ 20 DN below prev frame | process | DARK_OBJ |
| 10 | **QUIET** | `ratio` ≤ 0.25 | ≤ 75/300 tiles | `ratio`: (tiles darker than model with z > 3.0) / 300 | clouds | QUIET |
| 11 | **SCENE_DRIFT** | `dark_tiles` ≥ 4 **and** `new_dark_tiles` == 0 | dark_tiles 4–300; new_dark = 0 | `dark_tiles`: same as row 9; `new_dark_tiles`: same as row 9 | process | SCENE_DRIFT |
| 12 | **AMBIGUOUS** | default | — | — | process | AMBIGUOUS |

---

## 4. Calibration & Validator Notes

- **Validator:** `src/cloud-check/validate.py` — replays stored telemetry against the Python model; uses `validate_config.json` for threshold config and `display_spec.py` for field display names.
- **Burst validator:** `src/cloud-check/validate_burst.py` — replays burst filter offline including FAST_SHIFT and ISOLATED stages (not available on-device).
- **dt-based stages:** FAST_SHIFT and ISOLATED (in `burst_filter.py`) are skipped in the firmware validator — they fall through to the background model.
- **Lighting scenario:** `global_mean` at capture time determines exposure mode — `NORMAL` (≥ 130 DN) or `LOWLIGHT` (< 130 DN). Transmitted as `photo_mode` field and displayed in server gallery.
- **Tile threshold for display:** 20 DN (used in frame detail view for highlighting changed tiles).

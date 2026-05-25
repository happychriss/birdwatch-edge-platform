# Cloud-Detection Model — Design and Logic

The model decides, for each wakeup (PIR or RTC), whether the frame is a **real object** (upload tagged `process`) or a **false trigger / cloud shadow** (still uploaded, tagged `clouds`). The decision is biased toward upload: a missed bird is worse than a spurious cloud frame.

**All frames are uploaded regardless of decision** so the server's Python backfill can replay the background model in exact chronological order.

For the authoritative, up-to-date reference see **`requirements_model.md`**.

---

## Two-Layer Architecture

### Layer 1 — Burst Sequence Filter

> Source: `cloud_check.c` burst block + `src/cloud-check/cloud_check/burst_filter.py`

Compares each frame's JPEG-decoded YCbCr tile means to the **immediately preceding captured frame** (stored in NVS as `cc_p / cc_pu / cc_pv / cc_pgm`). Eliminates burst re-fires on the same sun/cloud transition before the background model runs.

| Stage | Condition | Decision |
|-------|-----------|----------|
| FIRST | No previous frame in NVS | process |
| BRIGHTNESS_SHIFT | \|gm_diff\| > 12 DN | process (whole-scene shift) |
| DUPLICATE | n_changed == 0 **AND n_chroma == 0** | clouds (pixel- and chroma-identical re-fire) |
| BRIGHT_STABLE | gm > 160 AND n_dark < 35 | clouds (bright, no shadow-casting object) |
| DIFFUSE | n_dark ≥ 60 tiles | clouds (cloud shadow sweeping whole scene) |
| SAFE | default | → background model |

`n_changed` = tiles where |Y_cur − Y_prev| > 12 DN.  
`n_chroma` = tiles where ΔU² + ΔV² > 64 vs prev frame.  
`n_dark` = tiles darkened by > 12 DN vs prev frame.

The chroma DUPLICATE gate (added Phase 3, 2026-05-25) fixes the frame 517/518 pigeon suppression: a pigeon produces ΔC ≈ 5.2 on its tile, well above the threshold, so it is never incorrectly suppressed.

FAST_SHIFT and ISOLATED stages exist in the Python reference but are omitted from firmware (no wall-clock time before WiFi/SNTP).

### Layer 2 — Background Model

> Source: `cloud_check.c` + `src/cloud-check/cloud_check/classifier.py`

Per-tile EMA background model with z-score anomaly detection on a **20×15 tile grid (300 tiles, 8×8 px each)**.

**Input:** tile means come from on-device JPEG decode via TJpgDec ROM (streaming BT.601 YCbCr accumulation — no raw YUV422 capture). No intermediate RGB buffer allocated.

**Model update policy:** only frames with `source == "rtc"` (15-min RTC cycle) update the background model. PIR-triggered frames are evidence-only.

---

## Photo-Bucket × Scene-Bucket Structure

The background model is indexed by two dimensions:

| Dimension | Name | Values | Derived from |
|-----------|------|--------|--------------|
| Outer | **`photo_bucket`** | `NORMAL` (80 ≤ gm < 160), `BRIGHT` (gm ≥ 160), `LOWLIGHT` (gm < 80) | LIGHTCHECK metering shot global_mean |
| Inner | **`scene_bucket`** | `0` (always — K=1 currently) | Forward-compatible slot for shadow geometry clustering |

Each photo-bucket has an independent EMA model (Y mean + U mean + V mean + variance + frames_seen counter). This replaces the old K=4 centroid-based bucket assignment (retired in Phase 3).

---

## Decision Pipeline (background model, in priority order)

| # | Stage | Condition | Decision |
|---|-------|-----------|----------|
| 1 | NIGHT | global_mean < 70 DN | process |
| 2 | WARMUP | frames_seen < 4 | process |
| 3 | DARK_OBJ | dark_tiles ≥ 1 AND new_dark_tiles ≥ 1 AND chroma_ok | process |
| 4 | QUIET | ratio ≤ 0.25 | **clouds (suppress)** |
| 5 | SCENE_DRIFT | dark_tiles ≥ 4 AND new_dark_tiles == 0 | process + re-calibrate |
| 6 | AMBIGUOUS | default | process |

`chroma_ok` per tile: ΔC² = ΔU² + ΔV² > 64 vs model mean, OR Y drop > 70 DN. This gate prevents pure-luma cloud shadows (stable chroma) from accumulating `dark_tiles`.

`frames_seen` increments only on RTC frames. WARMUP is the initial warmup period per photo-bucket; with 15-min RTC cycles, each bucket warms after 4 daylight reference frames (~1 hour for NORMAL/BRIGHT in good weather).

### Key Thresholds

| Parameter | Value | Location |
|-----------|-------|----------|
| Z-score gate | 3.0 | `CC_Z_THRESHOLD` |
| Quiet ratio | ≤ 0.25 | `CC_QUIET_RATIO` |
| DARK_OBJ model delta | ≥ 35 DN | `CC_DARK_DELTA_MODEL` |
| DARK_OBJ prev delta | ≥ 20 DN | `CC_DARK_DELTA_PREV` |
| Chroma DUPLICATE gate | ΔC² > 64 (ΔC > 8) | `BW_CC_CHROMA_DELTA_THR_SQ` |
| Chroma DARK_OBJ gate | ΔC² > 64 vs model | `BW_CC_CHROMA_DOBJ_GATE_SQ` |
| BRIGHT photo threshold | gm ≥ 160 | `BW_BRIGHT_PHOTO_THRESHOLD` |
| LOWLIGHT photo threshold | gm < 80 | `BW_LOWLIGHT_PHOTO_THRESHOLD` |
| Night threshold | gm < 70 | `CC_NIGHT_THRESHOLD` |
| Warmup frames | 4 | `CC_WARMUP_FRAMES` |

---

## Two-Phase Camera Pipeline (Phase 3)

1. **Metering shot** — `BW_CAM_MODE_LIGHTCHECK` (GRAYSCALE QQVGA, non-saturating AE profile). Compute `global_mean`. Derive `photo_bucket`.
2. **JPEG capture** — Re-init camera to `photo_bucket`'s exposure profile (JPEG SXGA). Capture one frame.
3. **On-device decode** — `bw_cam_jpeg_decode_to_tile_means()` via TJpgDec ROM. Produces 300-element Y/U/V arrays. ~300–400 ms.
4. **Cloud-check** — `bw_cc_assess()` with YUV tile means. Model updated only if `source == "rtc"`.
5. **Upload** — `bw_http_upload_image(meta, fb->buf, fb->len)`. Both process and clouds frames are uploaded.

On-device timing (SXGA path): ~1.4–1.5 s compute, excl. WiFi.

---

## Validation Results (2026-05-25, 147 labelled frames)

| Metric | Value |
|--------|-------|
| Non-cloud recall (birds/people) | **1.000** — zero misses |
| Cloud recall (false-trigger suppression) | **0.606** |

---

## Label Conventions

| Label | Meaning |
|-------|---------|
| `bird` | Confirmed bird in frame |
| `ignore` | No useful content (also used for "delete") |
| `special` | Unusual but not a bird |
| (none) | Unlabeled |

---

## Known Limitations

1. **LOWLIGHT warms slowly** — at 51.5°N, only ~15–25 min of civil twilight per day produce `gm < 80`. LOWLIGHT may stay in WARMUP for several days; warmup always uploads (safe bias).
2. **Blob check Python-only** — `dark_blob_max` (largest connected dark-delta region) runs in `backfill_meta.py` but not on-device. On-device DARK_OBJ uses the z+delta+chroma gate without the spatial blob cap.
3. **No boundary hysteresis** — a frame with `global_mean` near 80 or 160 may flap between photo-buckets on consecutive RTC cycles. Add hysteresis if observed in telemetry.

# Cloud-Detection Model — Design and Logic

The model decides, for each PIR-triggered camera frame, whether the motion was caused by a **cloud / shadow** (suppress upload) or a **real object** (upload to server). The decision is biased toward upload: a missed bird is worse than a spurious cloud frame.

---

## Two-Layer Architecture

### Layer 1 — Burst Sequence Filter (`cloud_check/burst_filter.py`, mirrored in firmware)

Compares the current frame to the **immediately preceding frame** (stored in NVS as raw tile means). Runs before the background model.

Eliminates **burst re-fires** on the same sun/cloud event: if the scene hasn't changed meaningfully since the last PIR trigger, there is no new object.

| Stage | Condition | Action |
|-------|-----------|--------|
| FIRST | No previous frame in NVS | Process (first ever) |
| BRIGHTNESS_SHIFT | \|gm_diff\| > 12 DN | Process (whole-scene shift — bird may coincide) |
| DUPLICATE | n_changed = 0 tiles | Suppress (pixel-identical re-fire) |
| BRIGHT_STABLE | gm > 160 AND n_dark < 35 | Suppress (bright scene, no shadow-casting object) |
| DIFFUSE | n_dark ≥ 60 tiles | Suppress (cloud shadow sweeping whole frame) |
| SAFE | default | Pass through to background model |

`n_dark` = tiles darkened by > 12 DN vs previous frame.
`n_changed` = tiles changed by > 12 DN in either direction.

FAST_SHIFT and ISOLATED stages exist in the Python reference but are omitted from firmware (no wall-clock time before WiFi/SNTP sync).

DIFFUSE is intentionally NOT in `_BURST_SUPPRESS_STAGES` — it falls through to the background model, which is better positioned to compare against the long-term mean.

### Layer 2 — Background Model (`cloud_check/classifier.py`, mirrored in firmware)

Per-tile **EMA (exponential moving average)** model updated only on accepted cloud frames (QUIET) and scene resets (SCENE_DRIFT, NIGHT). EMA alpha = 0.15 — deliberately slow so a single object frame cannot corrupt it.

---

## Lighting-Scenario Buckets (K=4)

Rather than time-of-day buckets (which require a clock and mis-classify overcast or seasonal shifts), frames are assigned to one of four **lighting-scenario buckets** by nearest L2 centroid over the full 300-tile vector.

Centroids were computed offline via k-means over 324 confirmed-background DB frames.

| Bucket | Description | Typical global mean |
|--------|-------------|---------------------|
| 0 | Dim / low-light / dawn-dusk | ≈ 57 DN |
| 1 | Mid-light | ≈ 102 DN |
| 2 | Mid-bright | ≈ 118 DN |
| 3 | Bright / direct sun | ≈ 155 DN |

Each bucket has its own independent EMA model (mean + variance per tile). The bucket assignment selects which model the current frame is compared against.

Model is pre-seeded from centroid means (not cold mean=128) with `bucket_seen = warmup_frames_per_bucket = 4` so the warmup window only fires for the first 4 genuinely novel frames per bucket.

---

## Tile Grid

**20 × 15 = 300 tiles** over the QQVGA (160×120) lightcheck capture → 8×8 pixel tiles.

The z-score per tile is: `z = (bucket_mean[tile] - frame_value[tile]) / sqrt(variance[tile])`.

`var_floor = 36` (std ≥ 6 DN). `init_var = 256` (std = 16 DN) until the model converges.

---

## Decision Pipeline (background model, in priority order)

All stages that are not QUIET return `process` (upload). QUIET returns `clouds` (suppress).

| # | Stage | Condition | Outcome |
|---|-------|-----------|---------|
| 1 | NIGHT | global_mean < 70 DN | process |
| 2 | WARMUP | bucket_seen < 4 | process |
| 3 | DARK_OBJ | dark_tiles ≥ 1 **and** (no prev OR new_dark_tiles ≥ 1) **and** dark_blob_max ≤ 40% of frame | process |
| 4 | SCENE_DRIFT | dark_tiles ≥ 4 **and** temporal available **and** new_dark_tiles < 1 | process + model reset |
| 5 | QUIET | ratio ≤ 0.25 | **clouds (suppress)** |
| 6 | AMBIGUOUS | default | process |

SCENE_DRIFT is checked **before** QUIET (stale model with many persistent dark tiles would otherwise trigger QUIET incorrectly).

### Key thresholds

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `dark_object_min_delta` | 20 DN | Tiles ≥ 20 DN darker than model mean (no z-gate) |
| `temporal_dark_delta` | 20 DN | Tiles ≥ 20 DN darker than previous frame |
| `tile_z_threshold` | 3.0 | z-score gate for QUIET ratio mask only |
| `quiet_anomaly_ratio` | 0.25 | ≤ 25% z-anomalous dark tiles → suppress |
| `scene_drift_min_tiles` | 4 | Minimum persistently-dark tiles to trigger SCENE_DRIFT |
| `dark_obj_max_blob_fraction` | 0.40 | Largest dark blob ≤ 40% of frame (≤ 120 tiles) for DARK_OBJ |
| `night_brightness_threshold` | 70 DN | Below this → NIGHT |

---

## Blob Check for DARK_OBJ

Birds create **spatially compact** dark regions; whole-scene shadows are diffuse and large.

`dark_blob_max` = size of the largest connected region where tiles are ≥ 20 DN below the model (absolute delta, no z-gate).

Data from 23 labeled bird frames:
- All bird DARK_OBJ frames: `dark_blob_max` ≤ 101 tiles (≤ 34% of frame)
- Large-shadow false-positive ignore frames: `dark_blob_max` = 136–199 tiles (≥ 45%)

Threshold: `dark_blob_max ≤ 0.40 × 300 = 120 tiles`. Frames exceeding this fall through to QUIET or AMBIGUOUS.

**Note:** This blob check is Python-only. It cannot easily be replicated in ESP32 firmware without dynamic memory allocation and connected-component analysis, so it runs during server-side backfill but not on-device.

---

## Model Update Rules

The background model is updated (EMA step) only when:
- `result == 'clouds'` (QUIET) — accepted background frame
- `trigger == 'SCENE_DRIFT'` — model is stale; update + reset warmup counter
- `trigger == 'NIGHT'` — scene too dark, no useful signal but model drift allowed

**NOT updated for:** DARK_OBJ, AMBIGUOUS, WARMUP, burst-suppress stages.

This prevents object frames (birds) from corrupting the background model.

---

## Performance (2026-05-23, 356 frames)

| Metric | Value | Notes |
|--------|-------|-------|
| Bird recall | **22/23 (95.7%)** | id=67 missed (QUIET suppressed — no dark blob) |
| Precision | Unknown | 333 frames unlabeled |
| Labeled "ignore" suppressed | ~35 of 94 labeled "ignore" | Many dawn/dusk model-mismatch frames still process (they upload anyway but are safe) |

### Label conventions

| Label | Meaning |
|-------|---------|
| `bird` | Confirmed bird in frame |
| `ignore` | No useful content (no bird; also used for "delete" = camera not set up) |
| `special` | Unusual but not a bird (shadow event, hardware test, etc.) |
| (none) | Unlabeled — frame was uploaded but not yet reviewed |

---

## Known Limitations

1. **Precision unmeasured** — 333/356 frames unlabeled. Cannot measure false positive rate.
2. **Bucket 1 / dim-scene gap** — frames at gm ≈ 76 map to bucket 1 (gm ≈ 102) due to L2 spatial distance, not gm proximity. No background frames at gm ≈ 76 exist yet, so model mismatch is large → many dark tiles from model staleness, not from a bird.
3. **Blob check not in firmware** — on-device DARK_OBJ fires on any 1+ dark tile without the blob cap. The Python backfill applies the stricter blob check retroactively.
4. **SCENE_DRIFT with bird** — if a bird causes SCENE_DRIFT (dark_tiles high, new_dark=0 because previous frame also had the bird), the model updates with the bird frame. In practice warmup handles most early-session frames and masks this case.

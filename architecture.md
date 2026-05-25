# System Architecture

## Overview

BirdWatch is a battery-powered outdoor wildlife camera (ESP32-S3) that:
1. Wakes on PIR motion
2. Runs an on-device cloud/shadow filter
3. Uploads bird photos over WiFi to a home server
4. Stores them in a PostgreSQL database visible via a Flask web gallery

---

## Hardware

- **Device:** Seeed XIAO ESP32-S3 Sense + OV2640 camera
- **Power:** LiPo cell → TPS22918 load switch → XIAO; PIR always-on (see `requirements.md §2`)
- **WiFi:** BSSID-pinned to Fritz!Box primary router (`b4:fc:7d:92:d4:90`)
- **Location:** Outdoor bird perch / feeder station

---

## Three-Project Code Structure

All source lives in `/workspace/src/`:

```
src/
  esp_bw_src/          # ESP32-S3 firmware (C, ESP-IDF v6)
    main/
      cloud_check.c    # on-device filter (burst + background model)
      cloud_check.h
      ...
  cloud-check/         # Python algorithm package + tools
    cloud_check/       # importable package
      classifier.py    # background-model decision tree (3-channel YUV, photo-bucket × scene-bucket)
      background.py    # EMA background model (shape: 3 photo-buckets × 1 scene-bucket × 15 × 20)
      burst_filter.py  # burst sequence filter (chroma-aware DUPLICATE)
      config.py        # all thresholds and config
      features.py      # tile extraction + BT.601 YCbCr conversion
      scene_buckets.py # K=1 placeholder (forward-compat for K>1)
    backfill_meta.py   # re-derive Y/U/V tile means + photo_bucket from stored JPEGs (PIL YCbCr)
    validate.py        # parity check: Python simulation vs ESP telemetry
    sweep.py           # grid search over thresholds
  python_bw_src/       # Flask web server + gallery
    main.py            # routes: /frame, /upload, /frames, /admin/...
    db.py              # SQLAlchemy models (BwFrame, BwPhoto, Session)
    display_spec.py    # field rendering rules for the gallery (badges, format_val, etc.)
    templates/         # Jinja2 HTML (frame_detail.html, frames.html, _meta_render.html)
```

**Consistency rule:** any change to the detection algorithm must be synced across all three projects. The Python reference is the truth; the ESP firmware is the deployed version.

---

## Database

**Production PostgreSQL** on the home server (`192.168.1.110`).

Both the dev container and the production server connect to the **same database**.

Connection configured via `.env` in `src/python_bw_src/` (not committed):
```
DATABASE_URL=postgresql://...
```

### Schema

Table `bw_frames`:
| Column | Type | Notes |
|--------|------|-------|
| `id` | serial PK | |
| `captured_at` | timestamp | When the ESP triggered |
| `result` | text | `'process'` or `'clouds'` |
| `filename` | text | JPEG filename on server |
| `meta` | JSONB | All other per-frame fields (see below) |

**Never add columns to `bw_frames`** — all new data goes into `meta` as JSONB keys.

Key `meta` fields:

| Field | Source | Notes |
|-------|--------|-------|
| `source` | firmware | `"pir"` (motion) or `"rtc"` (15-min reference cycle). Only RTC frames update background model. |
| `photo_bucket` | firmware / backfill | Exposure regime: `"NORMAL"`, `"BRIGHT"`, `"LOWLIGHT"` — derived from metering shot `global_mean` |
| `scene_bucket` | firmware / backfill | Shadow-pattern cluster index — always `0` (K=1 currently, forward-compat for K>1) |
| `global_mean` | firmware / backfill | Mean Y (luma) across all 300 tiles (0–255) |
| `tile_means` | firmware / backfill | 300-element uint8 array of Y tile means |
| `tile_means_u` | firmware / backfill | 300-element uint8 array of U (Cb) tile means, BT.601, centred at 128 |
| `tile_means_v` | firmware / backfill | 300-element uint8 array of V (Cr) tile means |
| `model_tile_means` | firmware / backfill | 300-element Y background model snapshot (before this frame's update) |
| `stage` | firmware / backfill | WARMUP, DARK_OBJ, QUIET, SCENE_DRIFT, AMBIGUOUS, NIGHT |
| `result` | firmware / backfill | `"process"` or `"clouds"` |
| `ratio` | firmware / backfill | dark_tiles / 300 |
| `dark_tiles` | firmware / backfill | Tiles with z > 3 AND ≥ 35 DN below model AND chroma gate |
| `new_dark_tiles` | firmware / backfill | Tiles with z > 3 AND ≥ 20 DN below previous frame |
| `n_chroma_changed` | firmware / backfill | Tiles where ΔC² > 64 vs background model mean |
| `dark_blob_max` | backfill only | Largest spatially-connected dark-delta region (tiles); Python-only, not on-device |
| `burst_trigger` | firmware / backfill | Burst pre-filter stage (FIRST, BRIGHTNESS_SHIFT, DUPLICATE, BRIGHT_STABLE, DIFFUSE, SAFE) |
| `burst_n_changed` | firmware | Tiles where \|Y_cur − Y_prev\| > 12 DN |
| `burst_n_dark` | firmware | Tiles darkened by > 12 DN vs prev frame |
| `burst_n_chroma` | firmware | Tiles where ΔU² + ΔV² > 64 vs prev frame |
| `label` | manual (gallery UI) | `bird`, `ignore`, `special`, or absent |
| `simulated` | backfill | True when meta was recomputed by `backfill_meta.py`, not emitted by firmware |
| `fresh_flash` | firmware | True on first frame after a new firmware flash |
| `battery` | firmware | Battery voltage in V |

---

## Production Server

**Address:** `192.168.1.110:8000`  
**Process:** Flask / gunicorn, started manually  
**Photo storage:** `/path/to/photos/` on server filesystem  
**JPG URL pattern:** `http://192.168.1.110:8000/static/<filename>`

The server receives uploads from the ESP32 at `/upload` and `/status`.

Gallery is visible at `http://192.168.1.110:8000/`.

---

## Dev Container

**Host:** `donald` (user `development`, running Claude Code)  
**Container:** Ubuntu, user `ubuntu`  
**ESP-IDF:** v6.0.1 at `/home/ubuntu/esp-idf/`  
**Python venv:** `src/python_bw_src/.venv/` — always `source .venv/bin/activate` before running scripts  
**Working dir:** `/workspace/`

The dev container has **no local JPG files**. When backfill needs tile_means it:
1. Reads them from DB `meta.tile_means` (preferred — stored by a prior backfill)
2. HTTP-fetches the JPG from `http://192.168.1.110:8000/static/<filename>` (fallback)

---

## Downloading / Accessing Frames

Photos live on the production server. To access them:
- **Gallery:** `http://192.168.1.110:8000/` — browse, label, view tile overlays
- **Frame detail:** `http://192.168.1.110:8000/frame/<id>` — full JPEG, tile heatmap, all meta fields
- **HTTP direct:** `http://192.168.1.110:8000/static/<filename>`
- **DB query:** run Python scripts from dev container — they connect to the shared production DB

---

## Labeling Frames

Labels are set in the gallery UI via keyboard shortcuts (in frame_detail view).

| Key | Label | Meaning |
|-----|-------|---------|
| `b` | `bird` | Confirmed bird in frame |
| `i` | `ignore` | Not useful — no bird, camera not set up, or "delete" |
| `s` | `special` | Interesting but not a bird |
| (clear) | (none) | Unlabeled |

**Note:** "delete" in conversation = `ignore` label. It means the frame should not be used as a positive training example. "Delete" does NOT mean remove from DB.

---

## Workflow: Algorithm Development

1. **Develop here** in dev container — edit Python files, run `backfill_meta.py` locally (writes to shared production DB)
2. **Verify** — query DB, check labeled frame stats, run `sweep.py`
3. **Push** to git — user pulls on production server and restarts Flask
4. **Flash ESP** — user flashes during active window: `idf.py -p /dev/ttyACM0 flash`

Never trigger production server endpoints (`/admin/backfill`) for algorithm work — run scripts locally.

---

## Flashing the Firmware

Cannot flash remotely — device is in deep sleep most of the time.

Build inside container (ESP-IDF v6):
```bash
source ~/esp-idf/export.sh
cd /workspace/src/esp_bw_src
idf.py build
```

User flashes manually during active window:
```bash
idf.py -p /dev/ttyACM0 flash
```

After flash: use pyserial to monitor serial output (idf_monitor needs interactive TTY).

**Never source the host IDF paths** (`/home/development/esp/esp-idf-v5.5/`) — they produce a v5.5 build directory visible inside the container that breaks the v6 build.

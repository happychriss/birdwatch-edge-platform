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
      classifier.py    # background-model decision tree
      background.py    # EMA background model
      burst_filter.py  # burst sequence filter
      config.py        # all thresholds and config
      features.py      # tile extraction from grayscale frames
      scene_buckets.py # K=4 centroid definitions
    backfill_meta.py   # recompute all frame meta from DB tile_means
    validate.py        # parity check: Python vs ESP telemetry
    sweep.py           # grid search over thresholds
  python_bw_src/       # Flask web server + gallery
    serve.py           # routes: /frame, /upload, /gallery, /admin/...
    db.py              # SQLAlchemy models (BwFrame, Session)
    display_spec.py    # field rendering rules for the gallery
    templates/         # Jinja2 HTML
    backfill_meta.py   → symlink or reference to ../cloud-check/backfill_meta.py
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
| `global_mean` | firmware / backfill | Mean brightness of all 300 tiles (0–255) |
| `tile_means` | firmware / backfill | Flat list of 300 tile means |
| `model_tile_means` | backfill | Model snapshot before this frame's update |
| `stage` | firmware / backfill | WARMUP, DARK_OBJ, QUIET, SCENE_DRIFT, AMBIGUOUS, NIGHT |
| `result` | firmware / backfill | `process` or `clouds` |
| `ratio` | backfill | Fraction of z-anomalous dark tiles |
| `dark_tiles` | backfill | Tiles ≥ 20 DN below model (no z-gate) |
| `new_dark_tiles` | backfill | Tiles ≥ 20 DN darker than previous frame |
| `dark_blob_max` | backfill | Largest connected dark-delta blob (tiles) |
| `scene_bucket` | backfill | K=4 lighting bucket (0=dim … 3=sun) |
| `burst_trigger` | firmware / backfill | Burst pre-filter stage for this frame |
| `label` | manual (gallery UI) | `bird`, `ignore`, `special`, or absent |
| `simulated` | backfill | True if meta was computed by backfill, not firmware |
| `fresh_flash` | firmware | True on first frame after a new build |
| `battery` | firmware | Battery voltage in V |
| `photo_mode` | firmware / backfill | `BRIGHT`, `NORMAL`, or `LOWLIGHT` |

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

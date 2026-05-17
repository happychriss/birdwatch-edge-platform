# BirdWatch Edge Platform

> A long-running ESP32-S3 wildlife camera, designed for ultra-low standby power, robust Wi-Fi connectivity, and an on-device false-trigger filter that runs before the upload decision.

The platform wakes on PIR motion, runs a lightweight scene-change classifier to decide whether the trigger was a real event (bird, person) or just sunlight shifting, and — only if something real was detected — captures a high-resolution JPEG and uploads it to the home server. Built to run unattended outdoors for months on a single LiPo.

---

## How it works

```
PIR detects motion
  → TPS22918 load switch cold-boots the XIAO ESP32-S3
    → capture VGA grayscale frame
      → cloud-check filter (< 10 ms, no WiFi)
          ├─ "cloud / lighting change" → power off immediately (battery saved)
          └─ "non-cloud / real event"
               → capture SXGA JPEG
                 → connect WiFi  (~1 s, NVS BSSID cache)
                   → POST image + battery voltage to home server
                     → Flask server runs bird detection
                       → board powers off completely  (~140 µA standby)
```

---

## Cloud-check filter

PIR sensors fire on any rapid infrared change — including clouds passing in front of the sun, wind moving leaves, or shadows sweeping across the scene. Without filtering, the majority of outdoor wildlife-camera events are empty frames.

The cloud-check filter is a **classical signal-processing pipeline** (not a neural network) that runs entirely on the ESP32-S3 in milliseconds:

1. **Tile the frame** — partition the 640×480 grayscale frame into a 16×12 grid of 40×40 px tiles.
2. **Compare against a learned background** — each tile has a per-time-of-day running mean and variance, updated continuously as the device operates. The z-score of each tile flags it as anomalous or normal.
3. **Temporal check** — newly anomalous tiles (not present in the previous capture) are a strong signal that something arrived. Persistent anomalies mean the scene simply changed between sessions and the model needs re-calibrating.
4. **Decision** — compact cluster of newly dark tiles → upload; scene matches model → suppress.

**Why classical, not ML?**

| Classical (this approach) | CNN / deep learning |
|--------------------------|---------------------|
| Every decision has a human-readable reason | Output is a probability score |
| No training data needed — model learns from live captures | Requires labelled images before deployment |
| Fits in 2 KB NVS; no weight file | Needs TFLite runtime + model file |
| Fully portable to bare-metal C | Requires ML framework |
| Adapts automatically to season/scene changes | Re-training needed when scene changes |

**Measured on 153 labelled field captures (online / self-calibrating mode):**

| Metric | Result |
|--------|--------|
| Non-cloud recall — birds and people never missed | **100 %** |
| Cloud recall — false triggers suppressed | **55 %** |

The model starts cold and calibrates itself over the first ~8 PIR events per day-period. After that it suppresses 55 % of sun/cloud false triggers with zero missed real events.

See [`src/cloud-check/`](src/cloud-check/README.md) for the Python simulation and [`requirements-cloud-detection.md`](requirements-cloud-detection.md) for the full algorithm specification.

---

## Telemetry pipeline & parity validation

The second major subsystem adds **schema-less per-frame telemetry** so any intermediate value computed on the ESP can be observed without changing the server or database schema.

### How it works

```
ESP  bw_tele_f("battery", v);          ← one-liner to add any value
     bw_tele_s("stage", "DARK_OBJ");
     bw_tele_arr_u8("tile_means", …);
       │
       ▼  multipart POST /frame   (image=JPEG  +  meta=<json>)
Flask /frame  ──► parse meta; store in bw_frames.meta (JSONB)
       │
       ├──► Gallery  — generic renderer, one display_spec.py entry per styled field
       └──► Validator — replays frames, diffs ESP values against Python classifier
```

**Adding a new observable value is a one-liner on the ESP.** It appears automatically in the database and web UI as a plain row. One entry in `display_spec.py` gives it a colour, badge, or format — no server or schema change.

### Database (`bw_frames`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer | primary key |
| `captured_at` | timestamp | from meta or server receive-time |
| `result` | varchar | promoted from `meta.result` for fast queries |
| `filename` | varchar | saved JPEG |
| `meta` | JSONB | all ESP telemetry keys verbatim |

The `bw_photos` table (legacy `/upload` path) is untouched.  
Run `python3 db.py` once after pulling to create `bw_frames`.

### Parity validator

Validates that the ESP's intermediate values exactly match a Python replay of the same classifier on the same inputs.

**Configuration** — `src/cloud-check/validate_config.json`:
```json
{
  "time_frame": { "from": "-24h", "to": "now" },
  "checks": [
    { "esp_key": "result",      "py_field": "label",         "type": "exact" },
    { "esp_key": "stage",       "py_field": "trigger",        "type": "exact" },
    { "esp_key": "dark_tiles",  "py_field": "dark_tiles",     "type": "int"   },
    { "esp_key": "ratio",       "py_field": "anomaly_ratio",  "type": "float", "tol": 0.001 }
  ]
}
```

Add or remove a checked value by editing `checks` — no code change.  
`time_frame` accepts ISO timestamps, `now`, `-24h`, `-7d`.

**Running:**
```bash
# From the web UI: Battery → Validate → Run validation
# Or from the command line:
cd src/cloud-check
.venv/bin/python validate.py [validate_config.json]
.venv/bin/python validate.py validate_config.json --json   # machine-readable
```

The validator automatically detects the most recent firmware flash (`fresh_flash` in meta) and starts its Python `BackgroundModel` from that point — ensuring both sides start from the same initial state.

**Results page** shows every checked value per frame: green = match, red = mismatch.

### Firmware flash detection

On every boot the firmware hashes its own build timestamp and compares against a value stored in NVS (`bw_meta/fw_hash`). On a new build:
1. Erases the NVS background model (`cc` namespace) so the ESP starts from the same clean priors as the Python validator
2. Emits `fresh_flash: true` + `fw_build: "..."` in the first frame's meta

This ensures parity validation agrees from frame 1 after every flash.

### Web UI

| Page | URL | Notes |
|------|-----|-------|
| Frame gallery | `/` | Main page — cards with badges, grid + list view |
| Frame detail | `/frame_detail?id=N` | Photo + collapsible meta table; **Tiles button** overlays the 16×12 tile grid on the photo, coloured by deviation from global mean |
| Battery | `/battery` | Hourly voltage chart + daily table |
| Validate | `/validate` | Run parity check; green/red per-value result table |

The frame detail **tile overlay** uses the same thresholds as the ESP firmware constants (`CC_DARK_DELTA_MODEL = 35`, `CC_DARK_DELTA_PREV = 20`): red tiles are dark-object candidates, orange is moderately dark, blue is anomalously bright. Values printed on each tile. Toggle with the **Tiles** button or `T` key.

---

## Platform vision

The current release covers the full capture-filter-upload loop. Planned extensions:

- **Cloud detection pipeline** — route confirmed detections to a cloud model for species classification
- **Post-processing pipeline** — species tagging, confidence scoring, time-series logging
- **Edge retraining** — feed field captures back into training on the home server

The firmware, filter, and server are kept deliberately modular so each layer can be improved independently.

---

## Repository layout

```
src/
├── esp_bw_src/       ESP32-S3 firmware  (ESP-IDF v6.0, C)
├── python_bw_src/    Home server        (Flask, Python 3)
├── cloud-check/      Cloud-check filter (Python simulation, see below)
└── scripts/          Build, flash, monitor, server scripts
training-data/        Field captures for model calibration  (images gitignored)
  real-data/clouds/             Empty-scene frames → label: cloud
  real-data/process-birds-pillow/  Pillow stand-in → label: non-cloud
  real-data/process-people/       Person visible   → label: non-cloud
external-docs/        Component datasheets
requirements.md       Full functional specification
requirements-cloud-detection.md   Cloud-check algorithm specification
```

| Sub-project | README |
|-------------|--------|
| ESP32-S3 firmware | [src/esp_bw_src/README.md](src/esp_bw_src/README.md) |
| Cloud-check filter | [src/cloud-check/README.md](src/cloud-check/README.md) |

---

## Hardware

| Component | Detail |
|-----------|--------|
| MCU + Camera | Seeed XIAO ESP32-S3 Sense (OV2640) |
| Power gate | TPS22918 load switch — board fully OFF between events |
| PIR sensor | Parallax 555-28027 Rev B |
| Battery monitor | 100 kΩ / 220 kΩ divider → ADC1, ~12 µA drain |
| Standby current | ~140 µA (PIR ~130 µA + divider ~12 µA; ESP32 fully off) |
| Battery | LiPo; runtime months at typical bird-activity event rates |

### Power latch (diode-OR gate)

Every wakeup is a **cold boot** via TPS22918 — no deep sleep resume, no state to corrupt. The firmware self-latches in the first instruction and releases power cleanly after each cycle.

```
LiPo+ ── TPS22918 ── XIAO BAT
              ON ◄─|── PIR OUT   (hardware trigger)
              ON ◄─|── GPIO5     (firmware self-latch)
```

---

## Firmware highlights

- **Self-latch first** — power hold asserted before any other code; crash before it = event lost, visible in the field
- **Cloud-check filter** — suppresses empty sun/cloud frames before WiFi wakes up; saves battery and gallery space
- **Cycle deadline watchdog** — 150 s FreeRTOS timer; always powers off even if hung
- **WiFi cold-boot optimised** — NVS BSSID/channel cache, PMK preserved, last-IP DHCP hint; ~1 s typical
- **Robust upload retry** — fully reconnects WiFi before each retry; handles mid-transfer drops cleanly
- **NVS checkpoints** — post-mortem on next boot shows how far the previous cycle got
- **Field blink codes** — no serial cable needed; count the LED blinks

| LED pattern | Meaning |
|-------------|---------|
| 4 rapid bursts | Boot confirmed (self-latch active) |
| 1 short | Milestone: cam OK / WiFi OK / upload OK / sleep |
| 2–8 long | Error — count the blinks: 2=cam init, 3=cam capture, 4=alloc, 5=no AP, 6=auth, 7=WiFi timeout, 8=upload |

---

## Quick start

```bash
# Flash firmware (ESP-IDF v6.0 required)
src/scripts/flash_firmware.sh [/dev/ttyACM0]
src/scripts/monitor.sh         # serial output with board reset

# Start the home server
src/scripts/start_server.sh    # Flask on port 8000

# Run the cloud-check filter simulation + gallery server
src/scripts/cloud-check.sh     # evaluates, then serves http://localhost:8001/gallery
```

Copy `src/esp_bw_src/main/credentials.h.template` to `credentials.h` and fill in your WiFi SSID/password before building. Set the server IP in `src/esp_bw_src/main/config.h`.

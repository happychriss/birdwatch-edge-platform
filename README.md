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

**Measured on 147 labelled field captures (online / self-calibrating mode):**

| Metric | Result |
|--------|--------|
| Non-cloud recall — birds and people never missed | **100 %** |
| Cloud recall — false triggers suppressed | **61 %** |

The model starts cold and calibrates itself over the first ~8 PIR events per day-period. After that it suppresses 61 % of sun/cloud false triggers with zero missed real events.

See [`src/cloud-check/`](src/cloud-check/README.md) for the Python simulation and [`requirements-cloud-detection.md`](requirements-cloud-detection.md) for the full algorithm specification.

---

## Platform vision

The current release covers the full capture-filter-upload loop. Planned extensions:

- **Cloud-check C port** — port the Python filter to `cloud_check.c` on the ESP32-S3; currently runs as a Python server queried over HTTP
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

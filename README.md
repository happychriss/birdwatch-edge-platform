# BirdWatch Edge Platform

> A long-running ESP32-S3 birdwatch platform, designed for low-power deployment, robust Wi‑Fi connectivity, and future edge-AI integration.

The platform wakes on PIR motion, captures a high-resolution JPEG, and uploads it to a home server for AI-based bird detection — all from a battery-powered device that draws ~140 µA in standby. Built to run unattended outdoors for months, with progressive AI capabilities added over time.

---

## Platform vision

This is an evolving platform, not a one-shot project. The current release covers the full capture-upload-detect loop. Planned extensions include:

- **On-device pre-filter** — reject empty frames before WiFi wakes up (saves power)
- **Cloud detection pipeline** — route detections to a cloud model for higher accuracy
- **Post-processing pipeline** — species tagging, confidence scoring, time-series logging
- **Edge retraining** — feed field captures back into training on the home server

The firmware and server are kept deliberately modular so each layer can be improved independently.

---

## How it works

```
PIR detects motion
  → TPS22918 load switch cold-boots the XIAO ESP32-S3
    → capture JPEG at 1280×960 (SXGA, OV2640)
      → connect WiFi  (~1 s, NVS BSSID cache)
        → POST image + battery voltage to home server
          → Flask server runs edge-AI bird detection
            → result stored, UI updated
              → board powers off completely  (~140 µA standby)
```

The server can also instruct the device to stay on and stream live MJPEG — useful for aiming the camera or verifying a detection.

---

## Repository layout

```
src/
├── esp_bw_src/       ESP32-S3 firmware  (ESP-IDF v6.0, C)
├── python_bw_src/    Home server        (Flask, Python 3)
└── scripts/          Build, flash, monitor, server start
training-data/        Field captures for retraining  (images gitignored)
external-docs/        Component datasheets
```

| Sub-project | README |
|-------------|--------|
| ESP32-S3 firmware | [src/esp_bw_src/README.md](src/esp_bw_src/README.md) |
| Flask server + AI  | [src/python_bw_src/](src/python_bw_src/) |

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
```

Copy `src/esp_bw_src/main/credentials.h.template` to `credentials.h` and fill in your WiFi SSID/password before building. Set the server IP in `src/esp_bw_src/main/config.h`.

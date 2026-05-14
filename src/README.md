# BirdWatch — PIR-triggered wildlife camera

A battery-powered outdoor wildlife camera that wakes on PIR motion, captures a JPEG, and uploads it to a home server over WiFi for AI bird detection.

Built on the **Seeed XIAO ESP32-S3 Sense** (ESP32-S3 + OV2640 camera). Two independent sub-projects — the ESP32 firmware and the Python server — are fully self-contained. See their own READMEs for details.

---

## What it does

```
PIR detects motion
  → TPS22918 load switch powers up the XIAO from zero
    → capture JPEG at 1280×960 (SXGA)
      → connect WiFi (~1 s with NVS BSSID cache)
        → POST image + metadata to home server
          → server runs AI bird detection
            → board powers off completely (~140 µA standby)
```

The server returns a mode flag: stay in **PIR sensor** mode (power off after upload) or switch to **camera server** mode (live MJPEG stream for monitoring).

---

## Sub-projects

| Path | Description |
|------|-------------|
| [`esp_bw_src/`](esp_bw_src/README.md) | ESP32-S3 firmware (ESP-IDF v6.0) |
| [`python_bw_src/`](python_bw_src/README.md) | Flask server — receives uploads, runs AI inference, serves web UI |

---

## Scripts

All operational scripts are in [`scripts/`](scripts/):

| Script | Purpose |
|--------|---------|
| `scripts/flash_firmware.sh [port]` | Build and flash the ESP32 firmware |
| `scripts/monitor.sh [port]` | Serial monitor via pyserial (resets board on start) |
| `scripts/start_server.sh` | Start (or restart) the Flask server on port 8000 |

Default port is `/dev/ttyACM0`. Pass an alternative as the first argument.

---

## Hardware

| Component | Detail |
|-----------|--------|
| MCU + Camera | Seeed XIAO ESP32-S3 Sense (OV2640) |
| Power gate | TPS22918 load switch — board is fully OFF between events |
| PIR sensor | Parallax 555-28027 Rev B — always powered, ~130 µA |
| Battery monitor | 100 kΩ / 220 kΩ voltage divider → GPIO2 (ADC1), ~12 µA drain |
| Standby current | ~140 µA total (PIR + divider; ESP32 fully off) |
| Battery | LiPo, monitored via ADC with eFuse curve-fit calibration |

### Power latch (diode-OR)

The TPS22918 ON pin is held by two Schottky diodes — one from PIR OUT, one from GPIO5 (firmware self-latch). Either source independently keeps the board powered.

```
LiPo+ ──── TPS22918 VIN → VOUT ──── XIAO BAT
               ON ◄─|─ PIR OUT    (D1 — hardware wakeup)
               ON ◄─|─ GPIO5/D4   (D2 — firmware self-latch)
               ON ──── 100 kΩ ──── GND
```

Every wakeup is a **cold boot** — not a deep sleep resume. After each cycle the firmware releases GPIO5; TPS22918 cuts power when the PIR pulse ends.

---

## Firmware highlights

- **Self-latch first** — `bw_power_init()` drives GPIO5 HIGH before any other code; a crash before it loses the event
- **Cycle deadline watchdog** — FreeRTOS 150 s one-shot timer; fires power release + deep sleep if any phase hangs
- **WiFi optimised cold-boot** — NVS BSSID/channel cache, PMK preserved, DHCP last-IP hint; ~1 s connect typical
- **Upload retry with WiFi reconnect** — each failed attempt fully reconnects WiFi before retrying (handles mid-transfer drops)
- **Camera server mode** — server can request device to stay on and serve live MJPEG (`/stream`, `/capture`, `/stop`)
- **Remote log** — optional HTTP POST of all ESP log lines to server `/log` endpoint (no serial cable needed in the field)
- **NVS checkpoints** — boot progress written to NVS; post-mortem on next boot shows how far the previous cycle got

### Field blink codes (no serial needed)

| LED pattern | Meaning |
|-------------|---------|
| 4 rapid bursts (30/70 ms) | Boot — fires immediately after self-latch |
| 1 short | Milestone: cam OK / WiFi OK / upload OK / sleep |
| 1 long | Watchdog fired |
| 2 long | Camera init failed |
| 3 long | Camera capture failed |
| 4 long | PSRAM alloc failed |
| 5 long | WiFi — no AP found |
| 6 long | WiFi — auth rejected |
| 7 long | WiFi — timeout |
| 8 long | Upload failed |

---

## Power budget

| State | Current |
|-------|---------|
| Standby (PIR + divider; ESP32 off) | ~140 µA |
| Active cycle (WiFi TX peak) | ~300 mA |
| Typical cycle duration | ~15–25 s |

---

## Documentation

Hardware schematics and reference documents are in [`/documentation/`](../documentation/).

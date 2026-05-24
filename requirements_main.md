# BirdWatch — Main Requirements

## Project Type

Follow `/workspace/skills/project-setup.md` for working conventions, folder structure, knowledge flow, and development workflow.

---

## 1. Overview

A battery-powered, outdoor wildlife camera that wakes on PIR motion, filters out false triggers caused by light changes, and uploads bird photos to a home server over WiFi.

**Status:** In progress  
**Source:** `/workspace/src/`  
**Platform:** Seeed XIAO ESP32-S3 Sense (ESP32-S3, OV2640 camera — see `requirements_hardware.md` §3 for note on newer boards)  
**Build system:** ESP-IDF v6.0.1 (`/workspace/src/esp_bw_src/`, project name `birdwatch`)

---

## 2. Cycle Lifecycle

```
PIR motion → TPS22918 ON → XIAO powers up
        │
        ▼
app_main(): bw_power_init() → GPIO5 HIGH (self-latch), blink × 1
        │
        ▼
check esp_sleep_get_wakeup_cause()
        │
        ├─ EXT1 (PIR)  ──────────────────────────────────────┐
        │   [fallback: board kept alive by USB after release]  │
        └─ UNDEFINED (normal TPS22918-triggered cold boot)    │
                                                              │
                    Read battery voltage                      │
                    Capture JPEG                              │
                    Connect WiFi                              │
                    Send image to server ◄────────────────────┘
                          │
                          ├─ Server replies CAMERA_SERVER mode
                          │       → start HTTP camera server
                          │         (live stream / capture endpoint)
                          │         wait until /stop endpoint hit
                          │
                          └─ Server replies PIR_SENSOR mode (default)
                                  → proceed to power down

        ├─ TIMER → send heartbeat status to server
        │
        └─ other → log error

Power down: camera deinit, remote log flush, WiFi off, GPIO5 LOW
           → TPS22918 cuts power immediately
           → esp_deep_sleep_start() as fallback if USB keeps board alive
```

Every boot is a full cold boot — no RTC memory or in-RAM state survives between events.

---

## 3. Watchdog & PIR Shutdown Sequencing

### 3.1 Cycle Deadline Watchdog

A FreeRTOS one-shot software timer (`bw_watchdog_start` / `bw_watchdog_stop` in `power.c`) guards every normal cycle:

- **Armed** in `app_main()` just before `run_normal_cycle()`, disarmed on normal completion.
- **Fires at** `BW_CYCLE_TIMEOUT_MS` (150 s) — calls `bw_power_release()` then `bw_power_deep_sleep_pir_wake()`.
- **Camera server mode:** watchdog is disarmed when the server requests live streaming; the server loop has its own tick-count timeout (`BW_CAM_SERVER_TIMEOUT_MS`, 10 min) to bound the open-ended user session.
- Task watchdog (TWDT) is deliberately not used — it feeds on `vTaskDelay()` so it cannot catch tight loops.

### 3.2 PIR State Monitoring

`bw_pir_is_active()` reads GPIO1 directly. PIR level is logged at two points per cycle:

1. Cycle start (`trigger=… pir=N`) — shows whether the sensor was still asserting at boot.
2. After WiFi up (`WiFi up — pir=N`) — shows whether motion persisted through capture.

### 3.3 Clean Shutdown — Wait for PIR Idle

`bw_wait_for_pir_idle(BW_PIR_IDLE_TIMEOUT_MS)` is called before `bw_power_release()`:

- If PIR is already LOW: release immediately.
- If PIR is still HIGH: poll every 200 ms up to `BW_PIR_IDLE_TIMEOUT_MS` (10 s) for it to drop.
- If it never drops within 10 s: release anyway (logged as a warning).

This prevents the TPS22918 from power-cycling and immediately re-triggering while the PIR output is still asserted. The cycle deadline watchdog remains armed throughout as the hard backstop.

---

## 4. Camera Server Mode

When the server instructs the device to enter camera server mode:

- A full HTTP camera server starts (`bw_camera_server_start()` in `camera_server.c`)
- Endpoints: `/` (web UI), `/capture`, `/stream` (MJPEG), `/status`, `/control`, `/stop`
- The device stays powered and running until `GET /stop` is called
- WiFi is then disconnected and the device powers down normally

---

## 5. Server Communication

- **Protocol:** HTTP POST to a home server (Python/Flask, port 8000)
- **Endpoints (device → server):**
  - `POST /upload` — JPEG image + metadata (battery, trigger reason, cc_label, cc_stage)
  - `POST /status` — heartbeat with battery and trigger info
- **Server address:** `192.168.1.110:8000` (`BW_TARGET_ZO 1`) or `192.168.1.100:8000` (default)
- **Trigger reasons sent:** `"PIR"`, `"Boot"`, `"Timer"`, `"Camera Start"`, `"Camera Stop"`
- **Return value** from `/upload` determines `global_mode` (`PIR_SENSOR=0` or `CAMERA_SERVER=1`)

---

## 6. NVS Usage

Two namespaces are used in Non-Volatile Storage:

| Namespace | Keys | Purpose |
|-----------|------|---------|
| `"cc"` | `cc_p` (tile means uint8), `cc_pgm` (global mean uint8), `frames_seen` | Cloud-check background model and previous burst frame |
| WiFi driver | BSSID cache | Pinned BSSID for Fritz!Box to avoid mesh repeater roaming |

The BSSID cache is cleared on a WiFi connect miss so the next boot goes straight to scan rather than wasting 10 s on a stale cache entry.

---

## 7. Blink Codes

The built-in LED (GPIO21, active-low) is used for status indication. Blink patterns are emitted at key lifecycle points:

- **1 blink** — `bw_power_init()` complete (self-latch asserted, boot confirmed)
- Additional patterns correspond to WiFi connect, upload success/failure — see `bw_led_blink()` calls in `main.c`.

---

## 8. Development Workflow

### 8.1 Build & Flash

- **Build (inside container):** `source /home/ubuntu/esp-idf/export.sh && cd /workspace/src/esp_bw_src && idf.py build`
- **Flash (user runs manually):** `source /home/ubuntu/esp-idf/export.sh && idf.py -p /dev/ttyACM0 flash`
- **Serial monitor:** pyserial on `/dev/ttyACM0` at 115200 baud — open immediately after every flash
- **IDF version:** v6.0.1 at `/home/ubuntu/esp-idf/` (container only — never use host IDF paths)

> You cannot flash remotely. The device spends almost all its time power-gated off. Your role: build and validate only. Tell the user the binary is ready; they flash manually.

### 8.2 Credentials & Config Flags

- **WiFi credentials:** `main/credentials.h` (not committed)
- **`BW_TARGET_ZO 1`** in `config.h` — switch to secondary server (`192.168.1.110`)
- **`BW_REMOTE_LOG`** — enable HTTP POST logging to port 8000 `/log`

### 8.3 WiFi Architecture

BSSID is pinned to the Fritz!Box primary router (`b4:fc:7d:92:d4:90`) — set in `config.h` as `BW_WIFI_BSSID`. This prevents roaming to a mesh repeater that caused `AUTH_FAIL` (reason=202) and `4WAY_HANDSHAKE_TIMEOUT` (reason=15) on most boots.

Key constraints:
- **No `esp_wifi_restore()`** — would clear the NVS PMK cache (slower WPA2 handshake)
- **`esp_reset_reason() == ESP_RST_POWERON`** guard on `bw_power_reboot_safe()` — bounds the reboot chain to exactly one soft reboot per PIR event
- **RTC GPIO domain in `bw_power_reboot_safe()`** — holds GPIO5 HIGH across `esp_restart()` so TPS22918 does not cut power during the ~500 ms bootloader window

See `skills/wifi-esp32s3.md` for full design rationale and timing budget.

---

## 9. Training Data

`/workspace/training-data/` (not committed beyond folder structure):

| Folder | Count | Label | Notes |
|--------|-------|-------|-------|
| `ignore-sun_shining/` | 224 | suppress | Noon sun false-triggers; burst filter target |
| `process-birds-pillow/` | 26 | process | Toy bird + pillow as proxy objects |
| `process-real-birds/` | 18 | process | Real bird captures (2026-05-21) |
| `process-people/` | 46 | process | Person legs/body in frame |
| `process-dark/` | 0 | process | Reserved for night/low-light captures |
| `duplicates/` | 36 | — | Byte-identical PIR triplets moved here; originals preserved |

All images are SXGA JPEG (from server), downsampled to 640×480 grayscale for burst filter evaluation, or 160×120 QQVGA for on-device processing.

---

## 10. Known Issues / Open Points

| Issue | Location | Notes |
|-------|----------|-------|
| OV3660 compatibility untested | `camera.c` | Newer XIAO boards ship with OV3660 instead of OV2640. Driver should be compatible but not verified |
| EXT1 wakeup is fallback only | `power.c` | `bw_power_deep_sleep_pir_wake()` configures EXT1 on GPIO1 — only reached when USB/bench power keeps board alive after TPS22918 release is ignored |

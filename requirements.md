# Project Requirements

## Project Type

Follow `/workspace/skills/project-setup.md` for working conventions, folder structure, knowledge flow, and development workflow.

---

## Bird Sensor — Functional Specification

**Status:** In progress
**Source:** `/workspace/src/`
**Platform:** Seeed XIAO ESP32-S3 Sense (ESP32-S3, OV2640 camera — see §2.4 for note on newer boards)
**Build system:** ESP-IDF v6.0.1 (`/workspace/src/esp_bw_src/`, project name `birdwatch`)

---

## 1. Overview

A battery-powered, outdoor wildlife camera that wakes on PIR motion, filters out false triggers caused by light changes, and uploads bird photos to a home server over WiFi.

---

## 2. Hardware Design

### 2.1 Power Architecture

The device is designed for ultra-low standby power. The XIAO ESP32-S3 board is **not** left in deep sleep — it is fully power-gated off between events because board leakage in sleep is too high for the battery goal.

A **TPS22918** load switch sits between the LiPo and the XIAO BAT input. The **Parallax 555-28027 Rev B PIR sensor** stays always powered directly from the LiPo (it has a long warm-up/calibration time and must not be switched).

```
LiPo+  ──── TPS22918 VIN
              TPS22918 VOUT ──── XIAO BAT
              TPS22918 ON   ◄─|─ PIR OUT  (D1: anode PIR, cathode ON)
                            ◄─|─ GPIO5/D4 (D2: anode GPIO5, cathode ON)
                            ──── 100 kΩ ──── GND
LiPo─  ──── common GND (PIR, TPS22918, XIAO)

PIR OUT ──┬── D1 ──── TPS22918 ON
          └── GPIO1 / D0  (ESP32 reads PIR state directly)
```

The TPS22918 ON node is a diode-OR gate — either PIR or GPIO5 holds it HIGH independently. Both paths use Schottky diodes so neither driver loads the other. The diode on the PIR path is essential: without it, PIR going LOW while GPIO5 is HIGH causes a current fight on the ON node that can release TPS22918 mid-cycle.

**Sequence:**

| Step | Actor | Event |
|------|-------|-------|
| 1 | PIR | Detects movement → output HIGH → D1 forward-biased → TPS22918 ON HIGH → XIAO powers up |
| 2 | ESP32 | `bw_power_init()` — first call in `app_main()` — drives GPIO5 HIGH → D2 holds TPS22918 ON regardless of PIR |
| 3 | ESP32 | Normal operation: capture, filter, upload |
| 4 | ESP32 | `bw_power_release()` drives GPIO5 LOW → D2 reverse-biased; if PIR also LOW → 100 kΩ pulls ON to GND → TPS22918 off |
| 5 | ESP32 | `esp_deep_sleep_start()` as **fallback only** — only reached when USB/bench power keeps the board alive after TPS22918 release is ignored |

> **Critical:** GPIO5 must be driven HIGH in the very first lines of `app_main()`. A crash before `bw_power_init()` means the self-latch never asserts; once the PIR pulse drops, the TPS22918 cuts power and the event is lost.

### 2.2 Pin Assignments

| Signal | GPIO | XIAO label | Notes |
|--------|------|------------|-------|
| PIR signal read / wakeup | 1 | D0 | `INPUT_PULLDOWN`; reads PIR OUT directly (before diode to TPS22918 ON); also EXT1 deep-sleep wakeup on fallback path |
| Battery ADC | 2 | D1 | `ADC1_CHANNEL_1`; voltage divider R1=10 kΩ, R2=20 kΩ → factor 1.5 × 1.1 cal |
| RF antenna select | 3 | D2 | Output; 0 = built-in, 1 = external U.FL; driven HIGH by `wifi_sta.c` during WiFi init |
| (unused) | 4 | D3 | Reserved / not connected |
| Power-hold / self-latch | 5 | D4 | Output; HIGH = hold TPS22918 ON, LOW = release → board loses power |
| Built-in LED | 21 | LED_BUILTIN | Status blinks; active-low on XIAO |
| Camera XCLK | 10 | — | OV2640/OV3660 clock |
| Camera I2C SDA | 40 | — | SCCB/I2C data |
| Camera I2C SCL | 39 | — | SCCB/I2C clock |
| Camera data D2–D9 | 15,17,18,16,14,12,11,48 | — | Parallel pixel bus |
| Camera VSYNC | 38 | — | |
| Camera HREF | 47 | — | |
| Camera PCLK | 13 | — | |

### 2.3 RF Antenna

GPIO3 (D2) selects the WiFi antenna on the XIAO ESP32-S3: 0 = built-in, 1 = external U.FL. `wifi_sta.c` drives it HIGH (external antenna) during WiFi init.

### 2.4 Camera

- **Sensor:** OV2640 on boards manufactured before ~2024 (JPEG, up to 1600×1200)
- **Newer boards use OV3660** (2048×1536). The ESP32-camera driver detects the sensor at runtime via SCCB PID. Current firmware is tested with OV2640 (PID=0x26 confirmed in logs). OV3660 is compatible with the same driver; verify if swapping boards.
- **Photo mode:** JPEG, FRAMESIZE_SXGA, quality 10 (high quality)
- **AWB:** wb_mode=2 (Cloudy/6500K fixed matrix) — avoids green-cast failure seen with auto-AWB in mixed outdoor light
- **AE:** ae_level=+1 EV, aec_value=450 — lifts foreground exposure in high-contrast sky scenes; AGC on
- **Light-check mode:** Grayscale, FRAMESIZE_QQVGA (160×120) — used for fast brightness comparison (currently disabled, see §8)
- **Frame discard:** 6 frames at 100 ms intervals before capture to let AEC/AGC converge (~10 fps sensor)

---

## 3. Wakeup & Trigger Logic

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

---

## 4. False-Trigger Filtering (Light Change Detection)

PIR sensors are sensitive to rapid infrared changes caused by moving shadows or cloud cover. The scene's sky region and moving plant/railing shadows on the tile floor are the dominant sources of false triggers.

### 4.1 Camera Grayscale Brightness Diff (implemented, currently disabled)

- `bw_light_check()` in `light_check.c` — takes a grayscale QQVGA frame, computes average pixel brightness, compares against the value stored in NVS from the previous wakeup.
- If the difference exceeds `BW_BRIGHTNESS_THRESHOLD` (15.0 on a 0–255 scale), the trigger is considered a light-change event and image upload is suppressed.
- **Currently disabled in `main.c`** — single global brightness is too coarse; it cannot distinguish a bird arriving from a cloud passing. Superseded by §4.2.

### 4.2 Cloud-Check Filter

> **Full specification:** [`requirements-cloud-detection.md`](requirements-cloud-detection.md)  
> **Python simulation:** [`src/cloud-check/`](src/cloud-check/README.md) — Phase 1 complete.

Binary on-device classifier: **"cloud"** (suppress upload) vs **"non-cloud"** (bird/person/anything-new → upload).

**Approach:** classical per-tile signal processing against an adaptive background model. Not ML — no weights, no training, pure integer arithmetic. Runs in < 10 ms on the ESP32-S3 once ported to C.

**Current results on 147 labelled real-scene frames (online, self-calibrating):**

| Metric | Value |
|--------|-------|
| Non-cloud recall (birds/people) | **1.000** — zero misses |
| Cloud recall (false-trigger suppression) | **0.606** — 61 % of cloud frames filtered |

**Decision pipeline (5 stages in priority order):**

1. **WARMUP** — bucket has < 8 observations → upload (model not yet reliable)
2. **DARK_OBJ** — tiles newly darker than both model and previous frame → upload (object arrived)
3. **QUIET** — ≤ 5 % of tiles anomalous → suppress (scene matches model)
4. **SCENE_DRIFT** — tiles dark vs model but not vs previous frame → upload + re-calibrate model (stale baseline)
5. **AMBIGUOUS** — default → upload

See [`requirements-cloud-detection.md`](requirements-cloud-detection.md) for full algorithm, parameter rationale, and the ESP-IDF port plan.

### 4.3 Training Data

`/workspace/training-data/` (not committed beyond folder structure):

| Folder | Count | Label | Notes |
|--------|-------|-------|-------|
| `real-data/sun/` | 109 | cloud | Empty balcony, varying sun/shadow, 2026-05 scene |
| `real-data/birds-simu/` | 11 | non-cloud | Same scene + small dark object as bird stand-in |
| `real-data/people/` | 27 | non-cloud | Same scene + person legs/hand in frame |
| `with-birds/` | 31 | non-cloud (aux) | 2025-07 scene, different camera angle and lighting — held out for cross-domain check, not mixed into training |

All images are 1600×1200 SXGA JPEG, downsampled to 640×480 grayscale inside the pipeline to match the on-device filter input.

---

## 5. Server Communication

- **Protocol:** HTTP POST to a home server (Python/Flask, port 8000)
- **Endpoints (device → server):**
  - `POST /upload` — JPEG image + metadata (battery, trigger reason, bright_diff)
  - `POST /status` — heartbeat with battery and trigger info
  - `POST /log` — JSON array of log lines (remote log feature, see §8)
- **Server address:** `192.168.1.110:8000` (`BW_TARGET_ZO 1`) or `192.168.1.100:8000` (default)
- **Trigger reasons sent:** `"PIR"`, `"Boot"`, `"Timer"`, `"Camera Start"`, `"Camera Stop"`
- **Return value** from `/upload` determines `global_mode` (`PIR_SENSOR=0` or `CAMERA_SERVER=1`)

---

## 6. Camera Server Mode

When the server instructs the device to enter camera server mode:
- A full HTTP camera server starts (`bw_camera_server_start()` in `camera_server.c`)
- Endpoints: `/` (web UI), `/capture`, `/stream` (MJPEG), `/status`, `/control`, `/stop`
- The device stays powered and running until `GET /stop` is called
- WiFi is then disconnected and the device powers down normally

---

## 7. Watchdog & PIR Shutdown Sequencing

### 7.1 Cycle Deadline Watchdog

A FreeRTOS one-shot software timer (`bw_watchdog_start` / `bw_watchdog_stop` in `power.c`) guards every normal cycle:

- **Armed** in `app_main()` just before `run_normal_cycle()`, disarmed on normal completion.
- **Fires at** `BW_CYCLE_TIMEOUT_MS` (150 s) — calls `bw_power_release()` then `bw_power_deep_sleep_pir_wake()`.
- **Camera server mode:** watchdog is disarmed when the server requests live streaming; the server loop has its own tick-count timeout (`BW_CAM_SERVER_TIMEOUT_MS`, 10 min) to bound the open-ended user session.
- Task watchdog (TWDT) is deliberately not used — it feeds on `vTaskDelay()` so it cannot catch tight loops.

### 7.2 PIR State Monitoring

`bw_pir_is_active()` reads GPIO1 directly. PIR level is logged at two points per cycle:

1. Cycle start (`trigger=… pir=N`) — shows whether the sensor was still asserting at boot.
2. After WiFi up (`WiFi up — pir=N`) — shows whether motion persisted through capture.

### 7.3 Clean Shutdown — Wait for PIR Idle

`bw_wait_for_pir_idle(BW_PIR_IDLE_TIMEOUT_MS)` is called before `bw_power_release()`:

- If PIR is already LOW: release immediately.
- If PIR is still HIGH: poll every 200 ms up to `BW_PIR_IDLE_TIMEOUT_MS` (10 s) for it to drop.
- If it never drops within 10 s: release anyway (logged as a warning).

This prevents the TPS22918 from power-cycling and immediately re-triggering while the PIR output is still asserted. The cycle deadline watchdog remains armed throughout as the hard backstop.

---

## 8. Known Issues / Open Points

| Issue | Location | Notes |
|-------|----------|-------|
| Camera brightness filter disabled | `main.c` | `bw_light_check()` wrapped in `#if 0`; `bright_diff` is always 0.0 sent to server. Being superseded by §4.2 cloud-check filter |
| OV3660 compatibility untested | `camera.c` | Newer XIAO boards ship with OV3660 instead of OV2640. Driver should be compatible but not verified |
| EXT1 wakeup is fallback only | `power.c` | `bw_power_deep_sleep_pir_wake()` configures EXT1 on GPIO1 — only reached when USB/bench power keeps board alive after TPS22918 release is ignored |

---

## 9. Development Workflow

- **Build:** `source /home/ubuntu/esp-idf/export.sh && cd /workspace/src/esp_bw_src && idf.py build`
- **Flash:** `source /home/ubuntu/esp-idf/export.sh && idf.py -p /dev/ttyACM0 flash`
- **Serial monitor:** pyserial on `/dev/ttyACM0` at 115200 baud; or open `http://192.168.1.100:8000/logs` for WiFi-based remote log viewer
- **Credentials:** WiFi SSID/password in `main/credentials.h` (not committed)
- **NVS:** Used to persist last camera brightness value across cycles (`namespace="storage"`, `key="last_avg"`)
- **Dev flags in `config.h`:**
  - `BW_DEV_NO_SLEEP 1` — skip GPIO5 LOW + deep sleep; device loops forever after cycle (keeps USB alive for monitoring)
  - `BW_REMOTE_LOG 1` — forward all `ESP_LOGx` output to server `/log` endpoint via HTTP POST
  - `BW_TARGET_ZO 1` — switch to secondary server (`192.168.1.110`)

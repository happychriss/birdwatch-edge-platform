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
- **Cloud-check mode:** Grayscale, FRAMESIZE_QQVGA (160×120) — one frame captured before the main JPEG for the cloud-check filter (§4.2); uses `CAMERA_GRAB_WHEN_EMPTY` to avoid frame-buffer overflow log noise
- **XCLK:** 16 MHz — tested stable; 20 MHz caused continuous FB-OVF and NO-EOI on SXGA JPEG (OV2640 JPEG compressor can't keep up at that PCLK for large frames)
- **Frame discard:** 6 frames at 100 ms intervals before the main JPEG capture to let AEC/AGC converge after the QQVGA→SXGA mode switch (~600 ms)
- **Exposure mode:** decided from cloud-check `global_mean` — `NORMAL` (≥130 DN) or `LOWLIGHT` (<130 DN, boosted AE/AGC); transmitted as `photo_mode` field and displayed in the server gallery

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

## 4. False-Trigger Filtering

PIR sensors are sensitive to rapid infrared changes caused by moving shadows or cloud cover. Two complementary filters are applied in sequence, both implemented on-device (C) and mirrored in Python for server-side validation.

### 4.1 Burst-Mode Sequence Filter (pre-filter, runs first)

> **Python reference:** `src/cloud-check/cloud_check/burst_filter.py`  
> **Evaluation:** `src/cloud-check/validate_burst.py`  
> **Status:** Implemented in ESP firmware (`cloud_check.c`) and Python server (`serve.py`). Validated 2026-05-23.

Compares each PIR event's QQVGA frame directly to the **previous captured frame** (stored in NVS as `cc_p` / `cc_pgm`). Suppresses PIR re-fires on the same sun/cloud transition before the background model runs.

**Decision pipeline (in order, no ML):**

| Stage | Condition | Decision |
|-------|-----------|----------|
| FIRST | no previous frame in NVS | process |
| BRIGHTNESS_SHIFT | \|gm_diff\| > 12 DN | process (whole-scene shift — bird could coincide) |
| DUPLICATE | n_changed ≤ 0 tiles | suppress (pixel-identical re-fire) |
| BRIGHT_STABLE | gm > 160 AND n_dark < 35 | suppress (bright scene, no shadow-casting object) |
| DIFFUSE | n_dark ≥ 60 tiles | suppress (cloud shadow sweeping entire scene) |
| SAFE | default | process (safety bias) |

Note: FAST_SHIFT and ISOLATED stages (from `burst_filter.py`) require `dt_seconds` which is unavailable on-device before WiFi/SNTP — omitted from firmware; validated offline via `validate_burst.py`.

**Results on training data (224 sun frames, 90 process frames, 2026-05-23):**
- Sun suppressed: **103/224 (46%)** — 0 errors
- Birds/pillow suppressed: **0/44** — 0 errors ✓
- People suppressed: 5/46 (acceptable; large body shadows exceed diffuse threshold)

### 4.2 Background-Model Pipeline (runs after burst filter passes)

> **Full specification:** [`requirements-cloud-detection.md`](requirements-cloud-detection.md)  
> **Python simulation:** [`src/cloud-check/`](src/cloud-check/README.md)

Per-tile EMA background model with z-score anomaly detection.

**Results on 147 labelled frames (online, self-calibrating):**

| Metric | Value |
|--------|-------|
| Non-cloud recall (birds/people) | **1.000** — zero misses |
| Cloud recall (false-trigger suppression) | **0.606** |

**Decision pipeline (in priority order):**
1. **NIGHT** — frame too dark (gm < 70) → process
2. **WARMUP** — < 4 frames seen → process
3. **DARK_OBJ** — tiles newly darker than both model and previous frame → process
4. **QUIET** — ≤ 25% dark-anomalous tiles → suppress
5. **SCENE_DRIFT** — dark vs model but not vs previous frame → process + re-calibrate
6. **AMBIGUOUS** — default → process

### 4.3 Full Pipeline Reference Table

Complete decision pipeline in execution order. Steps 1–6 are the burst pre-filter (compares to previous frame); steps 7–12 are the background-model pipeline, only reached when step 6 passes.

| # | Stage | Condition | Values / thresholds | Variable definitions | `result` | `stage` |
|---|---|---|---|---|---|---|
| 1 | **FIRST** | no previous frame in NVS | — | — | process | FIRST |
| 2 | **BRIGHTNESS_SHIFT** | `\|gm_diff\|` > 12 DN | 0–12 → continue; **> 12 → fires** | `gm_diff`: \|current frame mean − previous frame mean\| | process | BRIGHTNESS_SHIFT |
| 3 | **DUPLICATE** | `n_changed` == 0 | 0 → fires | `n_changed`: tiles where \|current − prev\| > 12 DN (any direction) | clouds | DUPLICATE |
| 4 | **BRIGHT_STABLE** | `gm` > 160 **and** `n_dark` < 35 | gm > 160 DN; n_dark 0–34 | `gm`: mean brightness of current frame (mean of 300 tiles); `n_dark`: tiles that got *darker* by > 12 DN vs prev | clouds | BRIGHT_STABLE |
| 5 | **DIFFUSE** | `n_dark` ≥ 60 | ≥ 60/300 tiles | `n_dark`: same as above | clouds | DIFFUSE |
| 6 | **SAFE** | default burst pass | n_dark 1–59, or gm ≤ 160 | — | → bg model | SAFE |
| 7 | **NIGHT** | `global_mean` < 70 | 0–69 DN | `global_mean`: mean of all 300 tile means (= `gm`) | process | NIGHT |
| 8 | **WARMUP** | `frames_seen` < 4 | 0–3 | `frames_seen`: non-NIGHT frames processed since last flash/reset | process | WARMUP |
| 9 | **DARK_OBJ** | `dark_tiles` ≥ 1 **and** `new_dark_tiles` ≥ 1 | both ≥ 1 | `dark_tiles`: tiles with z > 3.0 AND ≥ 35 DN below model mean; `new_dark_tiles`: tiles with z > 3.0 AND ≥ 20 DN below prev frame | process | DARK_OBJ |
| 10 | **QUIET** | `ratio` ≤ 0.25 | ≤ 75/300 tiles | `ratio`: (tiles darker than model with z > 3.0) / 300 | clouds | QUIET |
| 11 | **SCENE_DRIFT** | `dark_tiles` ≥ 4 **and** `new_dark_tiles` == 0 | dark_tiles 4–300; new_dark = 0 | `dark_tiles`: same as row 9; `new_dark_tiles`: same as row 9 | process | SCENE_DRIFT |
| 12 | **AMBIGUOUS** | default | — | — | process | AMBIGUOUS |

### 4.4 Training Data

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

## 5. Server Communication

- **Protocol:** HTTP POST to a home server (Python/Flask, port 8000)
- **Endpoints (device → server):**
  - `POST /upload` — JPEG image + metadata (battery, trigger reason, cc_label, cc_stage)
  - `POST /status` — heartbeat with battery and trigger info
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
| OV3660 compatibility untested | `camera.c` | Newer XIAO boards ship with OV3660 instead of OV2640. Driver should be compatible but not verified |
| EXT1 wakeup is fallback only | `power.c` | `bw_power_deep_sleep_pir_wake()` configures EXT1 on GPIO1 — only reached when USB/bench power keeps board alive after TPS22918 release is ignored |

---

## 9. Development Workflow

- **Build:** `source /home/ubuntu/esp-idf/export.sh && cd /workspace/src/esp_bw_src && idf.py build`
- **Flash:** `source /home/ubuntu/esp-idf/export.sh && idf.py -p /dev/ttyACM0 flash`
- **Serial monitor:** pyserial on `/dev/ttyACM0` at 115200 baud
- **Credentials:** WiFi SSID/password in `main/credentials.h` (not committed)
- **NVS:** Used by cloud_check (background model + previous frame, namespace `"cc"`) and WiFi (BSSID cache)
- **Dev flags in `config.h`:**
  - `BW_TARGET_ZO 1` — switch to secondary server (`192.168.1.110`)

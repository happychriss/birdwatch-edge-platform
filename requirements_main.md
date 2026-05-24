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

## 2. Wakeup Sources

The board has two independent wakeup triggers, both cold-boot via TPS22918:

| Source | Mechanism | Detected by |
|--------|-----------|-------------|
| **PIR** | Motion → PIR HIGH → D1 → TPS22918 ON | DS3231 Alarm-1 flag NOT set at boot |
| **DS3231 RTC** | Alarm → INT LOW → Q1 off → R3 pulls ON node HIGH → TPS22918 ON | DS3231 Alarm-1 flag SET at boot |

Wakeup source is read in the very first lines of `app_main()` (before flag is cleared) and sent as `"source": "pir"` or `"source": "rtc"` in the upload JSON.

## 3. Cycle Lifecycle

```
PIR motion OR DS3231 alarm → TPS22918 ON → XIAO powers up (cold boot)
        │
        ▼
app_main():
  bw_power_init() → GPIO5 HIGH (self-latch)
  i2cdev_init()   → one-time I2C subsystem init for entire cycle
  detect wakeup source (DS3231 alarm flag → "pir" or "rtc")
  rtc_compute_next() → s_next_wakeup_str for telemetry
  run_normal_cycle():
    │
    ├─ bw_cc_assess()   — QQVGA cloud-check filter
    ├─ bw_cam_capture() — SXGA JPEG
    ├─ bw_wifi_connect_blocking()
    ├─ rtc_sync_from_ntp() (weekly or after fresh flash)
    ├─ bw_http_upload_image(meta_json, jpg)
    │       meta includes: result, stage, global_mean, battery,
    │                      source, next_wakeup, photo_mode, ...
    │
    ├─── Server replies CAMERA_SERVER → live stream mode
    └─── Server replies PIR_SENSOR   → normal power-down
  rtc_compute_next() + rtc_arm_alarm() → DS3231 Alarm 1 set
  i2cdev_done()
  3s light sleep (cooldown — noise settling)
  bw_power_release() → GPIO5 LOW → TPS22918 cuts power
  esp_deep_sleep_start() as fallback (USB-powered bench only)
```

Every boot is a full cold boot — no RTC memory or in-RAM state survives between events.

---

## 4. Watchdog & PIR Shutdown Sequencing

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

## 5. Camera Server Mode

When the server instructs the device to enter camera server mode:

- A full HTTP camera server starts (`bw_camera_server_start()` in `camera_server.c`)
- Endpoints: `/` (web UI), `/capture`, `/stream` (MJPEG), `/status`, `/control`, `/stop`
- The device stays powered and running until `GET /stop` is called
- WiFi is then disconnected and the device powers down normally

---

## 6. Server Communication

- **Protocol:** HTTP POST to a home server (Python/Flask, port 8000)
- **Endpoints (device → server):**
  - `POST /frame` — multipart: `meta` (JSON) + `image` (JPEG)
  - `POST /status` — heartbeat with battery and trigger info
- **Server address:** `192.168.1.110:8000` (`BW_TARGET_ZO 1`) or `192.168.1.100:8000` (default)
- **Return value** from `/frame` determines `global_status` (`PIR_Sensor` or `Camera_Server`)

### 6.1 Upload JSON Fields (meta)

| Field | Type | Description |
|-------|------|-------------|
| `result` | string | `"clouds"` or `"process"` |
| `stage` | string | Cloud-check decision stage |
| `global_mean` | int | Scene brightness 0–255 |
| `source` | string | `"pir"` or `"rtc"` — what triggered this boot |
| `next_wakeup` | string | `"YYYY-MM-DD HH:MM:SS"` Berlin local — next RTC alarm |
| `battery` | float | Battery voltage (V) |
| `photo_mode` | string | `"NORMAL"`, `"BRIGHT"`, or `"LOWLIGHT"` |
| `trigger` | string | `"Boot"` (legacy field) |
| `fresh_flash` | bool | Present only on first cycle after reflash |
| `fw_build` | string | `__DATE__ __TIME__` build stamp (on fresh_flash only) |
| `burst_trigger` | string | Burst filter decision label |
| `tile_means` | uint8[] | 300-element tile mean array |

---

## 7. NVS Usage

| Namespace | Key | Type | Purpose |
|-----------|-----|------|---------|
| `"cc"` | `cc_p` | uint8 blob | Cloud-check tile means (background model) |
| `"cc"` | `cc_pgm` | uint8 | Previous global mean (burst filter) |
| `"cc"` | `frames_seen` | uint32 | Warmup counter |
| `"bw_wifi"` | `bssid` | 6-byte blob | Cached AP BSSID (cleared on miss) |
| `"bw_wifi"` | `channel` | uint8 | Cached AP channel (0 = scan) |
| `"bw_meta"` | `fw_hash` | uint32 | FNV-1a of build string; change triggers model reset |
| `"bw_meta"` | `rtc_sync` | uint32 | UTC epoch of last NTP sync |
| `"bw_meta"` | `cycle_min` | uint8 | RTC wakeup interval in minutes (default 15) |

The WiFi BSSID/channel cache is cleared on connect miss so the next boot scans fresh.

---

## 8. RTC Alarm Scheduling

The DS3231 Alarm 1 fires periodically to wake the board for time-lapse captures (independent of PIR motion).

- **Interval:** `BW_ALARM_CYCLE_MIN_DEFAULT = 15` min (overridable via NVS `bw_meta/cycle_min` u8)
- **Daylight bounds:** NOAA simplified solar algorithm for lat=51.5°N lon=10.0°E. Next alarm is clamped to `[sunrise, sunset)` UTC. If outside window, deferred to next sunrise.
- **Alarm arming:** Pre-computed before `run_normal_cycle()` (for telemetry), re-computed and armed post-cycle (post-NTP, more accurate). DS3231 stores local Berlin time; `mktime()` with `TZ=BW_TZ_BERLIN` converts correctly.
- **Log output:**
  ```
  MAIN: RTC now: 2026-05-25 14:22:00 local
  MAIN: alarm: DAYTIME — window 03:48–20:15 UTC, cycle=15 min
  MAIN: next wakeup → 2026-05-25 14:37:00 local
  MAIN: DS3231 alarm armed → 14:37:00
  ```

## 9. NTP Sync

DS3231 is synced from `pool.ntp.org` at most once per week (or immediately after a fresh flash). Sync runs inside `run_normal_cycle()` after WiFi is up.

- **Timezone:** `CET-1CEST,M3.5.0,M10.5.0/3` (Europe/Berlin, DST-aware)
- **NVS tracking:** `bw_meta/rtc_sync` stores UTC epoch of last sync
- **Timeout:** 10 s; if NTP unreachable, DS3231 keeps existing time

## 10. Blink Codes

The built-in LED (GPIO21, active-low) is used for status indication. Blink patterns are emitted at key lifecycle points:

- **1 blink** — `bw_power_init()` complete (self-latch asserted, boot confirmed)
- Additional patterns correspond to WiFi connect, upload success/failure — see `bw_led_blink()` calls in `main.c`.

---

## 11. Development Workflow

### 11.1 Build & Flash

- **Build (inside container):** `source /home/ubuntu/esp-idf/export.sh && cd /workspace/src/esp_bw_src && idf.py build`
- **Flash (user runs manually):** `source /home/ubuntu/esp-idf/export.sh && idf.py -p /dev/ttyACM0 flash`
- **Serial monitor:** pyserial on `/dev/ttyACM0` at 115200 baud — open immediately after every flash
- **IDF version:** v6.0.1 at `/home/ubuntu/esp-idf/` (container only — never use host IDF paths)

> You cannot flash remotely. The device spends almost all its time power-gated off. Your role: build and validate only. Tell the user the binary is ready; they flash manually.

### 11.2 Credentials & Config Flags

- **WiFi credentials:** `main/credentials.h` (not committed)
- **`BW_TARGET_ZO 1`** in `config.h` — switch to secondary server (`192.168.1.110`)
- **`BW_REMOTE_LOG`** — enable HTTP POST logging to port 8000 `/log`

### 11.3 WiFi Architecture

BSSID is pinned to the Fritz!Box primary router (`b4:fc:7d:92:d4:90`) — set in `config.h` as `BW_WIFI_BSSID`. This prevents roaming to a mesh repeater that caused `AUTH_FAIL` (reason=202) and `4WAY_HANDSHAKE_TIMEOUT` (reason=15).

**Two-round connect:** `bw_wifi_connect_blocking()` runs `try_connect()` once (up to 5 attempts, exponential backoff {2,3,5,7}s + jitter). On failure: 2s pause + fresh `esp_wifi_start()` → second `try_connect()`. Total WiFi budget: ≤ 52s.

**Timing budget:**

| Phase | Max time |
|-------|----------|
| WiFi round 1 (5 attempts + backoff) | 25 s |
| Soft reboot (if round 1 fails) | 0.5 s |
| WiFi round 2 | 25 s |
| HTTP retries (3 × 20 s) | 60 s |
| **Total** | **≤ 110.5 s** |

Watchdog deadline: `BW_CYCLE_TIMEOUT_MS = 150 s`.

Key constraints:
- **No `esp_wifi_restore()`** — would clear the NVS PMK cache (slower WPA2 handshake)
- **`esp_reset_reason() == ESP_RST_POWERON`** guard on `bw_power_reboot_safe()` — bounds the reboot chain to exactly one soft reboot per cold boot
- **RTC GPIO domain in `bw_power_reboot_safe()`** — holds GPIO5 HIGH across `esp_restart()` so TPS22918 does not cut power during the ~500 ms bootloader window

See `skills/wifi-esp32s3.md` for full design rationale and reason-code table.

---

## 12. Training Data

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

## 13. Known Issues / Open Points

| Issue | Location | Notes |
|-------|----------|-------|
| OV3660 compatibility untested | `camera.c` | Newer XIAO boards ship with OV3660 instead of OV2640. Driver should be compatible but not verified |
| EXT1 wakeup is fallback only | `power.c` | `bw_power_deep_sleep_pir_wake()` configures EXT1 on GPIO1 — only reached when USB/bench power keeps board alive after TPS22918 release is ignored |

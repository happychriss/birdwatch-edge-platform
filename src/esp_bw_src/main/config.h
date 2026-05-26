#pragma once
// ─── BirdWatch global configuration ──────────────────────────────────────────
// Pin assignments, server endpoints, thresholds.  Centralised here so other
// modules never hard-code values.

#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"

// ─── GPIO assignments (XIAO ESP32-S3 Sense) ────────────────────────────────
#define BW_PWR_HOLD_GPIO          GPIO_NUM_5   // D4 — hold TPS22918 ON pin high
#define BW_LED_BUILTIN_GPIO       GPIO_NUM_21  // built-in LED (active-low on XIAO)

// ─── DS3231 RTC (separate I2C bus from camera SCCB) ────────────────────────
// SDA=GPIO4/D3, SCL=GPIO6/D5; VCC on always-on BAT+ rail
#define BW_DS3231_SDA_GPIO        GPIO_NUM_4
#define BW_DS3231_SCL_GPIO        GPIO_NUM_6

// ─── ADC channels (ADC1 only — ADC2 is unusable when WiFi is on) ───────────
#define BW_ADC_UNIT               ADC_UNIT_1
#define BW_ADC_BATTERY_CHANNEL    ADC_CHANNEL_1   // GPIO2 (D1) — battery divider tap
#define BW_ADC_ATTEN              ADC_ATTEN_DB_12 // 0..~3.3V on the pin
#define BW_ADC_BITWIDTH           ADC_BITWIDTH_12

// ─── Battery divider (R1=100k, R2=220k → 1/0.6875 = 1.4545x) ───────────────
// V_pin = V_batt × 220/(100+220).  Calibrated ADC (curve-fitting, eFuse) is
// used — no empirical fudge factor.  Pin range: 2.06V (3.0V) … 2.89V (4.2V).
#define BW_BATT_DIVIDER_FACTOR    (320.0f / 220.0f)
#define BW_BATT_SAMPLE_COUNT      16   // 16 × 10ms = 160ms; reduces random noise by 4×
#define BW_BATT_SAMPLE_DELAY_MS   10

// ─── Server endpoint ─────────────────────────────────────────────────────────
// TARGET_ZO -> use second WiFi/server pair, otherwise primary.
#define BW_TARGET_ZO 1
#if BW_TARGET_ZO
  #define BW_SERVER_HOST "192.168.1.110"
#else
  #define BW_SERVER_HOST "192.168.1.100"
#endif
#define BW_SERVER_BASE "http://" BW_SERVER_HOST ":8000"
#define BW_UPLOAD_URL  BW_SERVER_BASE "/frame"
#define BW_STATUS_URL  BW_SERVER_BASE "/status"
// ─── HTTP client retry ──────────────────────────────────────────────────────
#define BW_HTTP_TIMEOUT_MS    20000
#define BW_HTTP_MAX_RETRIES   3
#define BW_HTTP_SOURCE        "BW_DEV"

// ─── WiFi connection ───────────────────────────────────────────────────────
#define BW_WIFI_MAX_RETRY     4       // 5 total attempts — after 2-3 hard failures the Fritz!Box
                                      // ban is active; fail fast and let reboot-once handle it
// 25s per round: covers 4 soft retries with exponential backoff {2,3,5,7}s + auth time.
// bw_wifi_connect_blocking() runs 2 rounds (hard radio reset between them) = 52s max WiFi.
// Was 40s single-round — two shorter rounds beat one long round because the Fritz!Box ban
// clears during the 2s inter-round gap and the fresh esp_wifi_start() resets driver state.
#define BW_WIFI_TIMEOUT_MS    25000
#define BW_WIFI_BACKOFF_MS   500  // pause between NVS-cache miss and fallback scan
// Retry backoff uses exponential delays with jitter — see s_backoff_ms[] in wifi_sta.c.

// Pin to a specific BSSID — connection skips scan/NVS-cache and never
// roams to mesh extenders.  Set all bytes to 0 to disable pinning and
// use scan-based connect instead.  MAC from Fritzbox UI: Home Network →
// Network → Network Connections (or sticker on the bottom of the Fritzbox).
#define BW_WIFI_BSSID         { 0xb4, 0xfc, 0x7d, 0x92, 0xd4, 0x90 }
#define BW_WIFI_COUNTRY_CC    "DE"    // regdomain: ch1–13, manual policy

// ─── Cycle deadline watchdog ────────────────────────────────────────────────
// Worst-case legitimate cycle: WiFi 52s (2×25s+2s) + 3 HTTP retries×20s + delays ≈ 114s.
// Set deadline above that so only a truly stuck cycle triggers the watchdog.
#define BW_CYCLE_TIMEOUT_MS     150000  // 150 s

// Camera server mode auto-shutdown if /stop never arrives.
#define BW_CAM_SERVER_TIMEOUT_MS 600000  // 10 min

// ─── RTC time sync from NTP ─────────────────────────────────────────────────
// Set to 1 to sync the DS3231 from NTP on the next boot (requires WiFi).
// Set to 0 (normal operation) to trust the RTC as-is.
// Workflow: set to 1 → build → flash → confirm serial log → set back to 0 → flash.
#define BW_RTC_SYNC_FROM_NTP  0

// Berlin timezone rule (CET/CEST) — used by alarm scheduling and NTP sync.
#define BW_TZ_BERLIN  "CET-1CEST,M3.5.0,M10.5.0/3"

// ─── Germany geolocation — sunrise/sunset for RTC alarm scheduling ─────────
// Central Germany: Berlin/Frankfurt approx.  Used by solar_utc_minutes() in main.c
// to bound periodic RTC wakeups to daylight hours only.
#define BW_GEO_LAT_DEG             51.5f   // degrees North
#define BW_GEO_LON_DEG             10.0f   // degrees East

// ─── Periodic RTC alarm cycle ───────────────────────────────────────────────
// Minutes between RTC-triggered wakeups during the evening capture window.
// Read from NVS key "cycle_min" (u8, namespace "bw_meta") at runtime; this is
// the fallback.
#define BW_ALARM_CYCLE_MIN_DEFAULT   15

// ─── Bird-active capture window ─────────────────────────────────────────────
// RTC wakeups active from sunrise through sunset + BW_BIRD_ACTIVE_WINDOW_POST_MIN.
// The post-sunset extension captures dusk bird-feeder activity under twilight.
// Outside [sunrise, sunset+POST_MIN) the alarm defers to the next sunrise.
//
// Approximate local times for the post-sunset extension:
//   Jan 16:40–17:10   Feb 17:20–18:00   Mar 18:10–19:50 (DST +1 h jump)
//   Apr 20:10–20:50   May 20:50–21:35   Jun 21:35–21:50
//   Jul 21:45–21:20   Aug 21:10–20:20   Sep 20:15–19:10
//   Oct 19:00–17:10 (DST −1 h jump)     Nov 16:50–16:30   Dec 16:25–16:35
#define BW_BIRD_ACTIVE_WINDOW_POST_MIN      30   // minutes after sunset

// ─── Post-cycle cooldown (light sleep before power release / reboot) ────────
// Halts CPU so residual switching noise does not extend the PIR pulse.
// GPIO5 pad latch holds HIGH during light sleep — TPS22918 stays on.
#define BW_COOLDOWN_SLEEP_US    3000000ULL  // 3 s

// ─── Camera light-mode switching ───────────────────────────────────────────
// global_mean from the cloud-check QQVGA frame (0-255).  Three modes:
//   BRIGHT    global_mean ≥ BW_BRIGHT_THRESHOLD  : full sun, protect sky from overexposure
//   NORMAL    BW_LOWLIGHT_THRESHOLD ≤ mean < BW_BRIGHT_THRESHOLD : typical daylight / overcast
//   LOWLIGHT  global_mean < BW_LOWLIGHT_THRESHOLD : dusk / dawn / dim interior
#define BW_BRIGHT_PHOTO_THRESHOLD    160
#define BW_LOWLIGHT_PHOTO_THRESHOLD   80

// ─── Cloud-check chroma thresholds ─────────────────────────────────────────
// Squared chroma distance (ΔU² + ΔV²) thresholds — avoids sqrt in inner loops.
// Linear equivalent: BW_CC_CHROMA_DELTA_THR_SQ = 64 → threshold = 8 DN.
#define BW_CC_CHROMA_DELTA_THR_SQ   64   // burst DUPLICATE gate: tile chroma-changed if ΔC² > this
#define BW_CC_CHROMA_DOBJ_GATE_SQ   64   // DARK_OBJ chroma gate: tile qualifies only if ΔC² > this

// ─── Camera AWB gain (Auto White Balance adaptive gain) ────────────────────
// Set to 1 to let the OV2640 adapt gain per-channel based on scene content.
// Set to 0 to use fixed Kelvin presets only (wb_mode per camera mode).
//
// WHY WE DISABLE (0): the installation has prominent green plants in frame.
// Auto AWB misidentifies the plant green as the neutral reference and fails
// to correct the OV2640's native green Bayer bias — making the cast worse.
// Fixed presets (Sunny/Cloudy) apply a known colour matrix and ignore scene
// content, which is more predictable for a fixed camera installation.
#define BW_CAM_AWB_GAIN  1

// ─── Global mode codes (matches python server reply field) ─────────────────
typedef enum {
    BW_MODE_ERROR        = -1,
    BW_MODE_PIR_SENSOR   =  0,
    BW_MODE_CAMERA_SERVER = 1,
} bw_mode_t;

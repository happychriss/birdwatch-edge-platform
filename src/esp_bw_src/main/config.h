#pragma once
// ─── BirdWatch global configuration ──────────────────────────────────────────
// Pin assignments, server endpoints, thresholds.  Centralised here so other
// modules never hard-code values.

#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"

// ─── GPIO assignments (XIAO ESP32-S3 Sense) ────────────────────────────────
#define BW_PWR_HOLD_GPIO          GPIO_NUM_5   // D4 — hold TPS22918 ON pin high
#define BW_LED_BUILTIN_GPIO       GPIO_NUM_21  // built-in LED (active-low on XIAO)

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
#define BW_TARGET_ZO 0
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
#define BW_WIFI_MAX_RETRY     4       // 5 total attempts × ~1.7s ≈ 8.5s — covers transient
                                      // 4-way handshake / auth glitches on the pinned AP
#define BW_WIFI_TIMEOUT_MS    10000   // 10s hard deadline per try_connect() call
#define BW_WIFI_BACKOFF_MS    500     // pause between NVS-cache miss and fallback scan

// Pin to a specific BSSID — connection skips scan/NVS-cache and never
// roams to mesh extenders.  Set all bytes to 0 to disable pinning and
// use scan-based connect instead.  MAC from Fritzbox UI: Home Network →
// Network → Network Connections (or sticker on the bottom of the Fritzbox).
#define BW_WIFI_BSSID         { 0xb4, 0xfc, 0x7d, 0x92, 0xd4, 0x90 }
#define BW_WIFI_CHANNEL       1       // Fritz!Box 2.4 GHz fixed channel
#define BW_WIFI_COUNTRY_CC    "DE"    // regdomain: ch1–13, manual policy

// ─── Cycle deadline watchdog ────────────────────────────────────────────────
// Worst-case legitimate cycle: WiFi 20s (2×10s) + 3 HTTP retries×20s + delays ≈ 82s.
// Set deadline above that so only a truly stuck cycle triggers the watchdog.
#define BW_CYCLE_TIMEOUT_MS     150000  // 150 s

// Camera server mode auto-shutdown if /stop never arrives.
#define BW_CAM_SERVER_TIMEOUT_MS 600000  // 10 min

// ─── Post-cycle cooldown (light sleep before power release / reboot) ────────
// Halts CPU so residual switching noise does not extend the PIR pulse.
// GPIO5 pad latch holds HIGH during light sleep — TPS22918 stays on.
#define BW_COOLDOWN_SLEEP_US    3000000ULL  // 3 s

// ─── Camera light-mode switching ───────────────────────────────────────────
// global_mean from the cloud-check QQVGA frame (0-255).  Below this value
// the firmware switches from PHOTO to PHOTO_LOWLIGHT (longer AEC integration,
// 32x gain ceiling, no AWB gain push, +2 EV) to improve early-morning and
// late-afternoon visibility without the green cast.  Tune based on field data.
#define BW_LOWLIGHT_PHOTO_THRESHOLD  130

// ─── Global mode codes (matches python server reply field) ─────────────────
typedef enum {
    BW_MODE_ERROR        = -1,
    BW_MODE_PIR_SENSOR   =  0,
    BW_MODE_CAMERA_SERVER = 1,
} bw_mode_t;

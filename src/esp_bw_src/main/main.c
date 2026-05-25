// ─── BirdWatch ESP32-S3 firmware — entry point & state machine ─────────────
//
// WAKEUP MODEL
// Every wakeup is a cold boot driven by the TPS22918 load switch:
//   PIR motion → TPS22918 ON → board powers up from zero.
// There is no deep sleep resume — EXT1 is not configured and GPIO1/D0 is
// not wired.  After each cycle the board releases GPIO5, TPS22918 cuts power
// when the PIR pulse ends, and the next PIR trigger is another cold boot.
// On USB (bench) the board stays powered; use BW_DEV_NO_SLEEP in config.h.
//
// CYCLE LIFECYCLE
//   1.  bw_power_init()      — IMMEDIATE: GPIO5 HIGH, self-latch TPS22918 on
//   2.  bw_blink(BOOT)       — 4 rapid blinks; if no blink = crash before latch
//   3.  bw_log_sysinfo() / wakeup cause
//   4.  nvs_flash_init()
//   5.  bw_adc_init() + read battery voltage
//   6.  bw_cam_init(PHOTO) + capture JPEG → copy to PSRAM → bw_cam_deinit()
//          → CAM_OK blink (1 short)  or error blink (2/3/4 long) + return
//   7.  bw_wifi_connect_blocking()
//          → WIFI_OK blink (1 short)  or error blink (5/6/7 long) + return
//   8.  bw_http_upload_image()
//          → UPLOAD_OK blink (1 short) or error blink (8 long)
//   9a. PIR_SENSOR    → tear down, sleep
//   9b. CAMERA_SERVER → reinit cam, start HTTP server, wait for /stop
//  10.  bw_wifi_disconnect(), bw_adc_deinit()
//  11.  SLEEP blink (1 short), bw_power_release() → TPS22918 cuts power
//
// FIELD BLINK REFERENCE
//   Boot  : 4 rapid bursts (30/70 ms) — always fires after self-latch
//   1 short: milestone — cam OK / WiFi OK / upload OK / sleep
//   N long : error — count the blinks:
//     1=watchdog  2=cam-init  3=cam-capture  4=cam-alloc
//     5=wifi-no-AP  6=wifi-auth  7=wifi-timeout  8=upload

#include <stdio.h>
#include <string.h>
#include <time.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_err.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"

#include "config.h"
#include "debug.h"
#include "power.h"
#include "adc_sense.h"
#include "wifi_sta.h"
#include "camera.h"
#include "cloud_check.h"
#include "http_client.h"
#include "camera_server.h"
#include "diag.h"
#include "telemetry.h"

#include <i2cdev.h>
#include <ds3231.h>
#if BW_RTC_SYNC_FROM_NTP
#include "esp_sntp.h"
#endif

static const char *TAG = "MAIN";

// ─── DS3231 NTP sync (manual, build-time opt-in) ─────────────────────────────
// Enabled by setting BW_RTC_SYNC_FROM_NTP 1 in config.h.
// Fetches NTP time and writes Berlin local time to DS3231 once per boot.
// Normal operation: BW_RTC_SYNC_FROM_NTP 0 — RTC is trusted as-is.

#if BW_RTC_SYNC_FROM_NTP
#define BW_NTP_SERVER     "pool.ntp.org"
#define BW_NTP_TIMEOUT_MS  10000

static void rtc_sync_from_ntp(void)
{
    ESP_LOGI(TAG, "DS3231 NTP sync → %s", BW_NTP_SERVER);
    setenv("TZ", BW_TZ_BERLIN, 1);
    tzset();

    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, BW_NTP_SERVER);
    esp_sntp_init();

    TickType_t t0 = xTaskGetTickCount();
    while (esp_sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED) {
        if ((xTaskGetTickCount() - t0) > pdMS_TO_TICKS(BW_NTP_TIMEOUT_MS)) {
            ESP_LOGW(TAG, "NTP sync timed out");
            esp_sntp_stop();
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    esp_sntp_stop();

    time_t now_utc = time(NULL);
    struct tm local_tm;
    localtime_r(&now_utc, &local_tm);
    ESP_LOGI(TAG, "NTP: %04d-%02d-%02d %02d:%02d:%02d CET/CEST",
             local_tm.tm_year + 1900, local_tm.tm_mon + 1, local_tm.tm_mday,
             local_tm.tm_hour, local_tm.tm_min, local_tm.tm_sec);

    i2c_dev_t rtc = {0};
    if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) == ESP_OK) {
        if (ds3231_set_time(&rtc, &local_tm) == ESP_OK)
            ESP_LOGI(TAG, "DS3231 updated from NTP");
        else
            ESP_LOGE(TAG, "DS3231 set_time failed");
        ds3231_free_desc(&rtc);
    }
}
#endif /* BW_RTC_SYNC_FROM_NTP */

// Compile-time build fingerprint — unique per rebuild (changes __DATE__/__TIME__).
static const char   s_fw_build[]  = __DATE__ " " __TIME__;
static bool         s_fresh_flash = false;   // set once in app_main when new firmware detected

// ─── Wakeup source ──────────────────────────────────────────────────────────
// Detected once at boot by reading the DS3231 alarm-1 flag.  If the flag is
// set the DS3231 fired the alarm (RTC wakeup); otherwise the PIR did.
typedef enum { BW_WAKE_PIR = 0, BW_WAKE_RTC } bw_wakeup_source_t;
static bw_wakeup_source_t s_wakeup_source = BW_WAKE_PIR;

// ─── Sunrise / sunset (NOAA simplified) ─────────────────────────────────────
// Returns minutes since midnight UTC for sunrise (is_sunset=false) or sunset.
// doy = 1..365.  Returns -1 on polar night, 24*60 on polar day (sunset only).
static int solar_utc_minutes(int doy, bool is_sunset)
{
    float b    = 2.0f * (float)M_PI * (doy - 1) / 365.0f;
    float eot  = 229.18f * (0.000075f
                 + 0.001868f * cosf(b) - 0.032077f * sinf(b)
                 - 0.014615f * cosf(2*b) - 0.04089f * sinf(2*b));
    float decl = 0.006918f - 0.399912f * cosf(b) + 0.070257f * sinf(b)
               - 0.006758f * cosf(2*b) + 0.000907f * sinf(2*b)
               - 0.002697f * cosf(3*b) + 0.00148f  * sinf(3*b);
    float lat    = BW_GEO_LAT_DEG * (float)M_PI / 180.0f;
    float cos_ha = (cosf(90.833f * (float)M_PI / 180.0f) - sinf(lat) * sinf(decl))
                 / (cosf(lat) * cosf(decl));
    if (cos_ha >  1.0f) return -1;
    if (cos_ha < -1.0f) return is_sunset ? 24*60 : 0;
    float ha   = acosf(cos_ha) * 180.0f / (float)M_PI;
    float noon = 720.0f - 4.0f * BW_GEO_LON_DEG - eot;
    return (int)(is_sunset ? noon + 4.0f * ha : noon - 4.0f * ha);
}

// Formatted RTC and next-wakeup times for telemetry (populated by rtc_compute_next()).
static char s_rtc_now_str[32]    = "?";
static char s_next_wakeup_str[32] = "?";

// Reads DS3231, computes next wakeup UTC clamped to daylight, logs the
// daytime/night decision, populates s_next_wakeup_str.  Returns UTC epoch
// (0 on error).  rtc must be a valid open i2c_dev_t.
static time_t rtc_compute_next(i2c_dev_t *rtc)
{
    struct tm now_local;
    if (ds3231_get_time(rtc, &now_local) != ESP_OK) {
        ESP_LOGE(TAG, "DS3231 get_time failed");
        return 0;
    }

    setenv("TZ", BW_TZ_BERLIN, 1);
    tzset();
    now_local.tm_isdst = -1;   // DS3231 has no DST state; let mktime determine from date
    time_t now_utc = mktime(&now_local);
    if (now_utc <= 0) {
        ESP_LOGE(TAG, "DS3231 time not set");
        return 0;
    }
    ESP_LOGI(TAG, "RTC now: %04d-%02d-%02d %02d:%02d:%02d local",
             now_local.tm_year+1900, now_local.tm_mon+1, now_local.tm_mday,
             now_local.tm_hour, now_local.tm_min, now_local.tm_sec);
    snprintf(s_rtc_now_str, sizeof(s_rtc_now_str),
             "%04d-%02d-%02d %02d:%02d:%02d",
             (now_local.tm_year+1900) % 10000,
             now_local.tm_mon+1, now_local.tm_mday,
             now_local.tm_hour, now_local.tm_min, now_local.tm_sec);

    uint8_t cycle_min = BW_ALARM_CYCLE_MIN_DEFAULT;
    {
        nvs_handle_t h;
        if (nvs_open("bw_meta", NVS_READONLY, &h) == ESP_OK) {
            nvs_get_u8(h, "cycle_min", &cycle_min);
            nvs_close(h);
        }
        if (cycle_min < 1) cycle_min = 1;
    }

    time_t next_utc = now_utc + (time_t)cycle_min * 60;

    struct tm gm;
    gmtime_r(&next_utc, &gm);
    int doy        = gm.tm_yday + 1;
    int next_utc_m = gm.tm_hour * 60 + gm.tm_min;

    int sunrise_m = solar_utc_minutes(doy, false);
    int sunset_m  = solar_utc_minutes(doy, true);
    if (sunrise_m < 0)     sunrise_m = 0;
    if (sunset_m > 24*60)  sunset_m  = 24*60;

    bool night = (next_utc_m < sunrise_m || next_utc_m >= sunset_m);

    if (night) {
        time_t day_start = (next_utc / 86400) * 86400;
        if (next_utc_m >= sunset_m) {
            day_start += 86400;
            gmtime_r(&day_start, &gm);
            doy       = gm.tm_yday + 1;
            sunrise_m = solar_utc_minutes(doy, false);
            if (sunrise_m < 0) sunrise_m = 0;
        }
        next_utc = day_start + (time_t)sunrise_m * 60;
        ESP_LOGI(TAG, "alarm: NIGHT — sunset %02d:%02d UTC → sunrise %02d:%02d UTC (doy %d)",
                 sunset_m/60, sunset_m%60, sunrise_m/60, sunrise_m%60, doy);
    } else {
        ESP_LOGI(TAG, "alarm: DAYTIME — window %02d:%02d–%02d:%02d UTC, cycle=%d min",
                 sunrise_m/60, sunrise_m%60, sunset_m/60, sunset_m%60, cycle_min);
    }

    struct tm next_local;
    localtime_r(&next_utc, &next_local);
    snprintf(s_next_wakeup_str, sizeof(s_next_wakeup_str),
             "%04d-%02d-%02d %02d:%02d:%02d",
             (next_local.tm_year+1900) % 10000,
             next_local.tm_mon+1, next_local.tm_mday,
             next_local.tm_hour, next_local.tm_min, next_local.tm_sec);
    ESP_LOGI(TAG, "next wakeup → %s local", s_next_wakeup_str);
    return next_utc;
}

// Arms DS3231 Alarm 1 at next_utc (Berlin local time stored in RTC).
static void rtc_arm_alarm(i2c_dev_t *rtc, time_t next_utc)
{
    struct tm next_local;
    localtime_r(&next_utc, &next_local);
    ds3231_clear_alarm_flags(rtc, DS3231_ALARM_1);
    ESP_LOGI(TAG, "DS3231 alarm 1 cleared — INT/SQW HIGH, Q1 ON, TPS22918 OFF");
    esp_err_t err = ds3231_set_alarm(rtc, DS3231_ALARM_1, &next_local,
                                     DS3231_ALARM1_MATCH_SECMINHOUR, NULL, 0);
    if (err == ESP_OK) {
        ds3231_enable_alarm_ints(rtc, DS3231_ALARM_1);
        ESP_LOGI(TAG, "DS3231 alarm armed → %02d:%02d:%02d",
                 next_local.tm_hour, next_local.tm_min, next_local.tm_sec);
    } else {
        ESP_LOGE(TAG, "ds3231_set_alarm failed: %s", esp_err_to_name(err));
    }
}

static const char *wakeup_to_trigger(uint32_t causes)
{
    // Every wakeup is a TPS22918 cold boot — EXT1 is not configured.
    (void)causes;
    return "Boot";
}

static bw_blink_code_t wifi_fail_blink(void)
{
    static const bw_blink_code_t tbl[] = {
        BW_BLINK_ERR_WIFI_NOT_FOUND,   // BW_WIFI_FAIL_NOT_FOUND
        BW_BLINK_ERR_WIFI_AUTH,         // BW_WIFI_FAIL_AUTH
        BW_BLINK_ERR_WIFI_TIMEOUT,      // BW_WIFI_FAIL_TIMEOUT
    };
    return tbl[bw_wifi_last_fail_reason()];
}

static void run_normal_cycle(void)
{
    uint32_t causes = esp_sleep_get_wakeup_causes();
    const char *trigger = wakeup_to_trigger(causes);
    ESP_LOGI(TAG, "─── normal cycle, trigger=%s source=%s ───",
             trigger, s_wakeup_source == BW_WAKE_RTC ? "RTC" : "PIR");

    if (bw_adc_init() != ESP_OK) {
        ESP_LOGE(TAG, "ADC init failed — continuing without sensor data");
    }
    float battery_v = bw_adc_read_battery_voltage();
    ESP_LOGI(TAG, "battery=%.3fV", battery_v);

    // ── Phase 1: Metering shot — LIGHTCHECK QQVGA → global_mean → photo_bucket ─
    // A cheap QQVGA grayscale frame is captured to determine scene brightness
    // before the full JPEG capture.  This avoids adapting the JPEG exposure from
    // a JPEG that hasn't been taken yet.
    bw_cam_mode_t photo_mode = BW_CAM_MODE_PHOTO;   // default: NORMAL
    uint8_t gm_lightcheck    = 128;

    if (bw_cam_init(BW_CAM_MODE_LIGHTCHECK) == ESP_OK) {
        camera_fb_t *lc = bw_cam_capture();
        if (lc && lc->len > 0) {
            uint32_t sum = 0;
            for (size_t i = 0; i < lc->len; i++) sum += lc->buf[i];
            gm_lightcheck = (uint8_t)(sum / lc->len);
        }
        if (lc) bw_cam_capture_return(lc);
        bw_cam_deinit();
    } else {
        ESP_LOGW(TAG, "LIGHTCHECK init failed — default NORMAL exposure");
    }

    if      (gm_lightcheck >= BW_BRIGHT_PHOTO_THRESHOLD)   photo_mode = BW_CAM_MODE_PHOTO_BRIGHT;
    else if (gm_lightcheck >= BW_LOWLIGHT_PHOTO_THRESHOLD) photo_mode = BW_CAM_MODE_PHOTO;
    else                                                    photo_mode = BW_CAM_MODE_PHOTO_LOWLIGHT;
    const char *photo_mode_str = (photo_mode == BW_CAM_MODE_PHOTO_BRIGHT)   ? "BRIGHT"   :
                                 (photo_mode == BW_CAM_MODE_PHOTO_LOWLIGHT) ? "LOWLIGHT" : "NORMAL";
    ESP_LOGI(TAG, "metering: gm=%u → photo_mode=%s", gm_lightcheck, photo_mode_str);

    // ── Phase 2: JPEG capture at the selected exposure profile ────────────────
    if (bw_cam_init(photo_mode) != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed");
        bw_diag_push("CAM_INIT_FAIL");
        bw_blink(BW_BLINK_ERR_CAM_INIT);
        bw_adc_deinit();
        return;
    }
    bw_cam_discard_frames(6, 100);   // AEC settle: ~600 ms at 16 MHz SXGA
    camera_fb_t *fb = bw_cam_capture();
    if (!fb) {
        ESP_LOGE(TAG, "no frame captured — aborting cycle");
        bw_diag_push("CAM_CAPTURE_FAIL");
        bw_blink(BW_BLINK_ERR_CAM_CAPTURE);
        bw_cam_deinit();
        bw_adc_deinit();
        return;
    }

    // ── Phase 3: On-device JPEG decode → per-tile YUV means ──────────────────
    // tile arrays live on the stack (300 B each — fine for ESP32-S3's 8 KB task stack)
    uint8_t tile_y[CC_NUM_TILES], tile_u[CC_NUM_TILES], tile_v[CC_NUM_TILES];
    bool decode_ok = (bw_cam_jpeg_decode_to_tile_means(
        fb->buf, fb->len, tile_y, tile_u, tile_v, CC_TILES_X, CC_TILES_Y) == ESP_OK);
    if (!decode_ok) {
        ESP_LOGW(TAG, "JPEG decode failed — treating frame as process (safety)");
    }

    // ── Phase 4: Cloud-check pipeline ─────────────────────────────────────────
    bw_cc_result_t cc;
    if (decode_ok) {
        bw_cc_set_source(s_wakeup_source == BW_WAKE_RTC);
        bw_cc_assess(tile_y, tile_u, tile_v, &cc);
    } else {
        // Decode failed: skip the model, upload unconditionally (safety bias)
        bw_tele_reset();
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "CAM_ERR");
        bw_tele_s("photo_bucket", photo_mode_str);
        bw_tele_i("global_mean", (long)gm_lightcheck);
        strcpy(cc.label, "process");
        strcpy(cc.stage, "CAM_ERR");
        strncpy(cc.photo_bucket, photo_mode_str, sizeof(cc.photo_bucket) - 1);
        cc.global_mean = gm_lightcheck;
    }
    ESP_LOGI(TAG, "cloud-check: bucket=%s gm=%u → %s (%s)",
             cc.photo_bucket, cc.global_mean, cc.label, cc.stage);

    // ── Phase 5: Suppress clouds — discard JPEG, skip upload ─────────────────
    if (strcmp(cc.label, "clouds") == 0) {
        bw_cam_capture_return(fb);
        bw_cam_deinit();
        bw_blink(BW_BLINK_CAM_OK);
        ESP_LOGI(TAG, "suppressed as clouds → no upload");
        bw_adc_deinit();
        return;
    }

    // ── Phase 6: Copy JPEG to PSRAM, stop camera ──────────────────────────────
    size_t   img_len = fb->len;
    uint8_t *img     = heap_caps_malloc(img_len, MALLOC_CAP_SPIRAM);
    if (!img) {
        ESP_LOGE(TAG, "PSRAM alloc failed (%u B) — aborting cycle", (unsigned)img_len);
        bw_diag_push("CAM_ALLOC_FAIL");
        bw_blink(BW_BLINK_ERR_CAM_ALLOC);
        bw_cam_capture_return(fb);
        bw_cam_deinit();
        bw_adc_deinit();
        return;
    }
    memcpy(img, fb->buf, img_len);
    bw_cam_capture_return(fb);
    bw_cam_deinit();

    bw_blink(BW_BLINK_CAM_OK);

    // ── WiFi up & upload ───────────────────────────────────────────────────
    if (bw_wifi_connect_blocking() != ESP_OK) {
        ESP_LOGE(TAG, "WiFi connect failed");
        static const char * const wifi_err[] = {
            "WIFI_NO_AP", "WIFI_AUTH_FAIL", "WIFI_TIMEOUT"
        };
        bw_diag_push(wifi_err[bw_wifi_last_fail_reason()]);
        bw_blink(wifi_fail_blink());
        free(img);
        bw_adc_deinit();
        ESP_LOGI(TAG, "cooldown sleep → reboot");
        esp_sleep_enable_timer_wakeup(BW_COOLDOWN_SLEEP_US);
        esp_light_sleep_start();
        // Reboot once to retry — a fresh stack resolves transient driver hangs
        // better than retrying within the same boot.  Only on a clean cold
        // boot (POWERON) so a software-reboot / panic / watchdog recovery
        // boot does not chain another reboot on top — bounding the chain to
        // exactly: cold boot → soft reboot → power off.
        if (esp_reset_reason() == ESP_RST_POWERON)
            bw_power_reboot_safe();
        return;
    }
    bw_blink(BW_BLINK_WIFI_OK);
    ESP_LOGI(TAG, "WiFi up");

#if BW_RTC_SYNC_FROM_NTP
    rtc_sync_from_ntp();
#endif

    // Flush any errors from previous failed cycles to the server now that we
    // have connectivity.  Posted as a status row (no image) in the debug field.
    if (bw_diag_has_errors()) {
        char diag_buf[512];
        bw_diag_get_log(diag_buf, sizeof(diag_buf));
        if (bw_http_post_status(battery_v, diag_buf) == ESP_OK) {
            bw_diag_clear();
        }
    }

    // Each failed attempt disconnects and reconnects WiFi before retrying so a
    // mid-transfer drop (e.g. MISSING_ACKS) does not strand the retry loop on
    // a dead connection.  Total attempts are bounded; the watchdog is the hard
    // backstop for the whole cycle.
    bw_mode_t mode = BW_MODE_ERROR;
    bool wifi_lost = false;
    for (int attempt = 1; attempt <= BW_HTTP_MAX_RETRIES; attempt++) {
        if (attempt > 1) {
            ESP_LOGW(TAG, "upload failed — reconnecting WiFi (attempt %d/%d)",
                     attempt, BW_HTTP_MAX_RETRIES);
            bw_wifi_disconnect();
            if (bw_wifi_connect_blocking() != ESP_OK) {
                ESP_LOGE(TAG, "WiFi reconnect failed — aborting upload");
                bw_diag_push("WIFI_RECONNECT_FAIL");
                wifi_lost = true;
                break;
            }
            ESP_LOGI(TAG, "WiFi back up");
        }
        ESP_LOGI(TAG, "upload attempt %d/%d", attempt, BW_HTTP_MAX_RETRIES);
        // Cloud-check values (result, stage, global_mean, photo_bucket, tile_means, …)
        // were already added inside bw_cc_assess().  Add the remaining cycle fields.
        bw_tele_f("battery",      (double)battery_v);
        bw_tele_i("wifi_rssi",    bw_wifi_get_rssi());
        bw_tele_s("trigger",      trigger);
        bw_tele_s("source",       s_wakeup_source == BW_WAKE_RTC ? "rtc" : "pir");
        bw_tele_s("rtc_time",     s_rtc_now_str);
        bw_tele_s("next_wakeup",  s_next_wakeup_str);
        if (s_fresh_flash) {
            bw_tele_b("fresh_flash", true);
            bw_tele_s("fw_build",   s_fw_build);
        }
        mode = bw_http_upload_image(bw_tele_json(), img, img_len);
        if (mode != BW_MODE_ERROR) break;
    }
    free(img);

    if (wifi_lost) {
        ESP_LOGE(TAG, "WiFi lost during upload");
        bw_blink(wifi_fail_blink());
    } else if (mode == BW_MODE_ERROR) {
        ESP_LOGE(TAG, "upload failed after %d attempts", BW_HTTP_MAX_RETRIES);
        bw_diag_push("UPLOAD_FAIL");
        bw_blink(BW_BLINK_ERR_UPLOAD);
        bw_http_post_status(0.0f, "Error sending image");
    } else if (mode == BW_MODE_CAMERA_SERVER) {
        ESP_LOGI(TAG, "server requested CAMERA_SERVER mode");
        bw_http_post_status(battery_v, "Camera Start");
        bw_watchdog_stop();  // camera server is user-controlled; loop has its own timeout

        // Camera was stopped after capture; reinit for live streaming.
        if (bw_cam_init(BW_CAM_MODE_PHOTO) == ESP_OK) {
            bw_camera_server_start();
            TickType_t t0 = xTaskGetTickCount();
            while (!bw_camera_server_should_stop() &&
                   (xTaskGetTickCount() - t0) < pdMS_TO_TICKS(BW_CAM_SERVER_TIMEOUT_MS)) {
                vTaskDelay(pdMS_TO_TICKS(200));
            }
            bw_camera_server_stop();
            bw_cam_deinit();
        } else {
            ESP_LOGE(TAG, "camera reinit for server failed");
        }
        bw_http_post_status(battery_v, "Camera Stop");
    } else {
        ESP_LOGI(TAG, "server requested PIR_SENSOR mode");
        bw_blink(BW_BLINK_UPLOAD_OK);
        }

    bw_wifi_disconnect();
    bw_adc_deinit();
}

void app_main(void)
{
    // Step 1 — KEEP POWER ON.  Anything that crashes before this
    // call results in immediate power loss and lost diagnostics.
    ESP_ERROR_CHECK(bw_power_init());
    bw_blink_init();
    bw_blink(BW_BLINK_BOOT);

    esp_log_level_set("*", ESP_LOG_INFO);
    esp_log_level_set(TAG, ESP_LOG_VERBOSE);

    bw_log_sysinfo(TAG);
    bw_log_wakeup_cause(TAG);

    // NVS (used by cloud_check diagnostics, and WiFi).
    esp_err_t nvs = nvs_flash_init();
    if (nvs == ESP_ERR_NVS_NO_FREE_PAGES || nvs == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS needs erase — wiping");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    } else {
        ESP_ERROR_CHECK(nvs);
    }
    bw_diag_init();

    // Single i2cdev_init() for the entire power-on cycle.  i2cdev uses a
    // static bool that prevents re-init after a done(); calling init/done in
    // pairs across multiple functions leaves the mutex NULL on the second init.
    if (i2cdev_init() != ESP_OK)
        ESP_LOGE(TAG, "i2cdev_init failed — DS3231 unavailable this cycle");

    // ── Wakeup source detection ──────────────────────────────────────────────
    // Read DS3231 Alarm-1 flag BEFORE it is cleared.  If the flag is set the
    // DS3231 INT line fired (RTC periodic wakeup); otherwise the PIR triggered.
    {
        i2c_dev_t rtc = {0};
        if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) == ESP_OK) {
            ds3231_alarm_t fired = DS3231_ALARM_NONE;
            ds3231_get_alarm_flags(&rtc, &fired);
            if (fired & DS3231_ALARM_1) s_wakeup_source = BW_WAKE_RTC;
            ds3231_free_desc(&rtc);
        }
        ESP_LOGI(TAG, "wakeup source: %s",
                 s_wakeup_source == BW_WAKE_RTC ? "RTC" : "PIR");
    }

    // ── Firmware-update detection ────────────────────────────────────────────
    // FNV-1a hash of the compile-time build string.  On a change (new flash):
    //   • erase the NVS background model so the ESP starts from the same
    //     CC_INIT_MEAN / CC_INIT_VAR priors as a fresh Python validator run
    //   • set s_fresh_flash so the first upload carries fresh_flash=true,
    //     which the server records as a BwFlashEvent (anchor for the validator)
    {
        uint32_t h = 2166136261u;  // FNV offset basis
        for (const char *p = s_fw_build; *p; p++) { h ^= (uint8_t)*p; h *= 16777619u; }
        nvs_handle_t hm;
        if (nvs_open("bw_meta", NVS_READWRITE, &hm) == ESP_OK) {
            uint32_t stored = 0;
            nvs_get_u32(hm, "fw_hash", &stored);
            if (stored != h) {
                ESP_LOGI(TAG, "new firmware detected (%s) — resetting background model", s_fw_build);
                bw_cc_reset();
                nvs_set_u32(hm, "fw_hash", h);
                nvs_commit(hm);
                s_fresh_flash = true;
            }
            nvs_close(hm);
        }
    }

    // Pre-compute next wakeup before the cycle so it is available in telemetry.
    // (NTP may slightly shift RTC during the cycle; the alarm is re-computed
    //  and armed post-cycle for accuracy.  The telemetry value is close enough.)
    {
        i2c_dev_t rtc = {0};
        if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) == ESP_OK) {
            rtc_compute_next(&rtc);   // populates s_next_wakeup_str
            ds3231_free_desc(&rtc);
        }
    }

    bw_watchdog_start(BW_CYCLE_TIMEOUT_MS);
    run_normal_cycle();
    bw_watchdog_stop();
    bw_log_sysinfo(TAG);

    // ── DS3231 RTC — arm next periodic alarm ─────────────────────────────────
    // Re-compute post-cycle (post-NTP) for accuracy, then arm Alarm 1.
    // Done BEFORE the cooldown sleep so it is visible in the serial log (the
    // USB-CDC connection drops during light sleep and output after wake is lost).
    {
        i2c_dev_t rtc = {0};
        if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) != ESP_OK) {
            ESP_LOGE(TAG, "DS3231 not found — wakeup alarm not set");
        } else {
            time_t next_utc = rtc_compute_next(&rtc);
            if (next_utc > 0) rtc_arm_alarm(&rtc, next_utc);
            ds3231_free_desc(&rtc);
        }
    }
    i2cdev_done();   // paired with the single i2cdev_init() at the top of app_main

    ESP_LOGI(TAG, "cooldown sleep → power release");
    esp_sleep_enable_timer_wakeup(BW_COOLDOWN_SLEEP_US);
    esp_light_sleep_start();

    bw_power_release();
    bw_power_deep_sleep();
}

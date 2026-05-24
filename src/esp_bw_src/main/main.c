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
#include "esp_sntp.h"

static const char *TAG = "MAIN";

// ─── DS3231 NTP sync ──────────────────────────────────────────────────────────
// Syncs the DS3231 RTC from NTP once per week or after a firmware flash.
// Stores Berlin local time in the RTC (Europe/Berlin = CET/CEST).
// Tracks last-sync epoch in NVS key "rtc_sync" (namespace "bw_meta").

#define BW_NTP_SERVER         "pool.ntp.org"
#define BW_NTP_TIMEOUT_MS     10000
#define BW_RTC_SYNC_INTERVAL  (7u * 24u * 3600u)  // seconds
#define BW_TZ_BERLIN          "CET-1CEST,M3.5.0,M10.5.0/3"

// Returns true if DS3231 time is > 7 days past last NVS-recorded sync, or never synced.
static bool rtc_sync_needed(bool force)
{
    if (force) return true;

    uint32_t last_sync = 0;
    nvs_handle_t h;
    if (nvs_open("bw_meta", NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u32(h, "rtc_sync", &last_sync);
        nvs_close(h);
    }
    if (last_sync == 0) return true;

    setenv("TZ", BW_TZ_BERLIN, 1);
    tzset();
    bool needs = true;
    if (i2cdev_init() == ESP_OK) {
        i2c_dev_t rtc = {0};
        if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) == ESP_OK) {
            struct tm t;
            if (ds3231_get_time(&rtc, &t) == ESP_OK) {
                time_t ds_now = mktime(&t);
                needs = (ds_now <= 0) || ((ds_now - (time_t)last_sync) >= (time_t)BW_RTC_SYNC_INTERVAL);
            }
            ds3231_free_desc(&rtc);
        }
        i2cdev_done();
    }
    return needs;
}

// Fetches time from NTP, writes Berlin local time to DS3231, updates NVS last-sync.
// Must be called with WiFi connected.
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

    if (i2cdev_init() != ESP_OK) {
        ESP_LOGE(TAG, "i2cdev_init failed — DS3231 not updated");
        return;
    }
    i2c_dev_t rtc = {0};
    if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) == ESP_OK) {
        if (ds3231_set_time(&rtc, &local_tm) == ESP_OK) {
            nvs_handle_t h;
            if (nvs_open("bw_meta", NVS_READWRITE, &h) == ESP_OK) {
                nvs_set_u32(h, "rtc_sync", (uint32_t)now_utc);
                nvs_commit(h);
                nvs_close(h);
            }
        }
        ds3231_free_desc(&rtc);
    }
    i2cdev_done();
}

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

// ─── Set next DS3231 alarm ───────────────────────────────────────────────────
// Reads DS3231 current time, adds cycle_min (NVS or default), clamps to
// daylight window (sunrise..sunset UTC for Germany).  Clears alarm flag and
// arms Alarm 1.  rtc must be a valid open i2c_dev_t.
static void rtc_set_next_alarm(i2c_dev_t *rtc)
{
    struct tm now_local;
    if (ds3231_get_time(rtc, &now_local) != ESP_OK) {
        ESP_LOGE(TAG, "DS3231 get_time failed — alarm not set");
        return;
    }

    setenv("TZ", BW_TZ_BERLIN, 1);
    tzset();
    time_t now_utc = mktime(&now_local);   // local → UTC epoch
    if (now_utc <= 0) {
        ESP_LOGE(TAG, "DS3231 time not set — alarm not set");
        return;
    }

    // Cycle period: NVS "bw_meta"/"cycle_min" (u8), default BW_ALARM_CYCLE_MIN_DEFAULT.
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

    // Day-of-year and minutes-since-midnight-UTC for the candidate wakeup time.
    struct tm gm;
    gmtime_r(&next_utc, &gm);
    int doy         = gm.tm_yday + 1;
    int next_utc_m  = gm.tm_hour * 60 + gm.tm_min;

    int sunrise_m = solar_utc_minutes(doy, false);
    int sunset_m  = solar_utc_minutes(doy, true);

    // If outside daylight, advance to sunrise (same day or next day).
    if (sunrise_m < 0) sunrise_m = 0;       // polar day fallback
    if (sunset_m > 24*60) sunset_m = 24*60;

    if (next_utc_m < sunrise_m || next_utc_m >= sunset_m) {
        // Past sunset → push to tomorrow's sunrise.
        time_t day_start = (next_utc / 86400) * 86400;
        if (next_utc_m >= sunset_m) {
            day_start += 86400;
            gmtime_r(&day_start, &gm);
            doy       = gm.tm_yday + 1;
            sunrise_m = solar_utc_minutes(doy, false);
            if (sunrise_m < 0) sunrise_m = 0;
        }
        next_utc = day_start + (time_t)sunrise_m * 60;
        gmtime_r(&next_utc, &gm);
        next_utc_m = gm.tm_hour * 60 + gm.tm_min;
    }

    // Convert target UTC back to Berlin local time for DS3231.
    struct tm next_local;
    localtime_r(&next_utc, &next_local);

    ds3231_clear_alarm_flags(rtc, DS3231_ALARM_1);
    esp_err_t err = ds3231_set_alarm(rtc, DS3231_ALARM_1, &next_local,
                                     DS3231_ALARM1_MATCH_SECMINHOUR, NULL, 0);
    if (err == ESP_OK) {
        ds3231_enable_alarm_ints(rtc, DS3231_ALARM_1);
        ESP_LOGI(TAG, "DS3231 alarm → %04d-%02d-%02d %02d:%02d:%02d local"
                 " (cycle=%d min  sunrise=%02d:%02d sunset=%02d:%02d UTC)",
                 next_local.tm_year+1900, next_local.tm_mon+1, next_local.tm_mday,
                 next_local.tm_hour, next_local.tm_min, next_local.tm_sec,
                 cycle_min, sunrise_m/60, sunrise_m%60, sunset_m/60, sunset_m%60);
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
    ESP_LOGI(TAG, "─── normal cycle, trigger=%s ───", trigger);

    if (bw_adc_init() != ESP_OK) {
        ESP_LOGE(TAG, "ADC init failed — continuing without sensor data");
    }
    float battery_v = bw_adc_read_battery_voltage();
    ESP_LOGI(TAG, "battery=%.3fV", battery_v);

    // ── Cloud-check filter ─────────────────────────────────────────────────
    // Runs on a QQVGA grayscale frame before the main JPEG capture.
    // In debug mode the upload always proceeds; the decision is sent as
    // metadata (cc_label / cc_stage) so the server can display it.
    // To suppress uploads for cloud frames: check result.label == "clouds".
    bw_cc_result_t cc;
    if (bw_cc_assess(&cc) != ESP_OK) {
        ESP_LOGW(TAG, "cloud-check failed — proceeding with upload");
        strcpy(cc.label, "process");
        strcpy(cc.stage, "CAM_ERR");
    }

    // ── Capture JPEG ───────────────────────────────────────────────────────
    // Pick photo exposure mode from the ambient brightness the cloud-check
    // frame already measured — no extra camera cycle needed.
    bw_cam_mode_t photo_mode;
    if      (cc.global_mean >= BW_BRIGHT_PHOTO_THRESHOLD)   photo_mode = BW_CAM_MODE_PHOTO_BRIGHT;
    else if (cc.global_mean >= BW_LOWLIGHT_PHOTO_THRESHOLD) photo_mode = BW_CAM_MODE_PHOTO;
    else                                                     photo_mode = BW_CAM_MODE_PHOTO_LOWLIGHT;
    const char *photo_mode_str = (photo_mode == BW_CAM_MODE_PHOTO_BRIGHT)   ? "BRIGHT"   :
                                 (photo_mode == BW_CAM_MODE_PHOTO_LOWLIGHT) ? "LOWLIGHT" : "NORMAL";
    ESP_LOGI(TAG, "photo mode: %s (global_mean=%u bright>=%d normal>=%d)",
             photo_mode_str, cc.global_mean,
             BW_BRIGHT_PHOTO_THRESHOLD, BW_LOWLIGHT_PHOTO_THRESHOLD);

    if (bw_cam_init(photo_mode) != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed");
        bw_diag_push("CAM_INIT_FAIL");
        bw_blink(BW_BLINK_ERR_CAM_INIT);
        bw_adc_deinit();
        return;
    }
    bw_cam_discard_frames(6, 100);   // AEC settle after LIGHTCHECK→PHOTO mode switch (~600 ms at 16 MHz SXGA)
    camera_fb_t *fb = bw_cam_capture();
    if (!fb) {
        ESP_LOGE(TAG, "no frame captured — aborting cycle");
        bw_diag_push("CAM_CAPTURE_FAIL");
        bw_blink(BW_BLINK_ERR_CAM_CAPTURE);
        bw_cam_deinit();
        bw_adc_deinit();
        return;
    }

    // Copy JPEG into a standalone PSRAM buffer and stop the camera immediately
    // so the cam_task does not keep filling DMA buffers during the WiFi phase.
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

    // Sync DS3231 from NTP once per week or after a fresh flash.
    if (rtc_sync_needed(s_fresh_flash))
        rtc_sync_from_ntp();

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
        // photo_mode_str already set above
        // Append per-cycle values that are only known after cc assess completes.
        // Cloud-check values (result, stage, global_mean, ratio, tile_means, …) were
        // already added inside bw_cc_assess().  These three are added once here.
        bw_tele_f("battery",    (double)battery_v);
        bw_tele_s("trigger",    trigger);
        bw_tele_s("source",     s_wakeup_source == BW_WAKE_RTC ? "rtc" : "pir");
        bw_tele_s("photo_mode", photo_mode_str);
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

    // ── Wakeup source detection ──────────────────────────────────────────────
    // Read DS3231 Alarm-1 flag BEFORE it is cleared.  If the flag is set the
    // DS3231 INT line fired (RTC periodic wakeup); otherwise the PIR triggered.
    {
        if (i2cdev_init() == ESP_OK) {
            i2c_dev_t rtc = {0};
            if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) == ESP_OK) {
                ds3231_alarm_t fired = DS3231_ALARM_NONE;
                ds3231_get_alarm_flags(&rtc, &fired);
                if (fired & DS3231_ALARM_1) s_wakeup_source = BW_WAKE_RTC;
                ds3231_free_desc(&rtc);
            }
            i2cdev_done();
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

    bw_watchdog_start(BW_CYCLE_TIMEOUT_MS);
    run_normal_cycle();
    bw_watchdog_stop();
    bw_log_sysinfo(TAG);

    // ── DS3231 RTC — arm next periodic alarm ─────────────────────────────────
    // Done BEFORE the cooldown sleep so it is visible in the serial log (the
    // USB-CDC connection drops during light sleep and output after wake is lost).
    // Alarm time = now + cycle_min (NVS), clamped to daylight (sunrise..sunset).
    if (i2cdev_init() == ESP_OK) {
        i2c_dev_t rtc = {0};
        if (ds3231_init_desc(&rtc, I2C_NUM_0, BW_DS3231_SDA_GPIO, BW_DS3231_SCL_GPIO) != ESP_OK) {
            ESP_LOGE(TAG, "DS3231 not found — wakeup alarm not set");
        } else {
            rtc_set_next_alarm(&rtc);
            ds3231_free_desc(&rtc);
        }
        i2cdev_done();
    } else {
        ESP_LOGE(TAG, "i2cdev_init failed — wakeup alarm not set");
    }

    ESP_LOGI(TAG, "cooldown sleep → power release");
    esp_sleep_enable_timer_wakeup(BW_COOLDOWN_SLEEP_US);
    esp_light_sleep_start();

    bw_power_release();
    bw_power_deep_sleep();
}

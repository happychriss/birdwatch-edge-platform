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

static const char *TAG = "MAIN";

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
    bw_cam_mode_t photo_mode = (cc.global_mean < BW_LOWLIGHT_PHOTO_THRESHOLD)
                               ? BW_CAM_MODE_PHOTO_LOWLIGHT
                               : BW_CAM_MODE_PHOTO;
    ESP_LOGI(TAG, "photo mode: %s (cc.global_mean=%u threshold=%d)",
             photo_mode == BW_CAM_MODE_PHOTO_LOWLIGHT ? "LOWLIGHT" : "NORMAL",
             cc.global_mean, BW_LOWLIGHT_PHOTO_THRESHOLD);

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
        const char *photo_mode_str = (photo_mode == BW_CAM_MODE_PHOTO_LOWLIGHT) ? "LOWLIGHT" : "NORMAL";
        mode = bw_http_upload_image(battery_v, trigger, cc.label, cc.stage, photo_mode_str, img, img_len);
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

    bw_watchdog_start(BW_CYCLE_TIMEOUT_MS);
    run_normal_cycle();
    bw_watchdog_stop();
    bw_log_sysinfo(TAG);
    ESP_LOGI(TAG, "cooldown sleep → power release");
    esp_sleep_enable_timer_wakeup(BW_COOLDOWN_SLEEP_US);
    esp_light_sleep_start();
    bw_power_release();
    bw_power_deep_sleep();
}

#include "camera.h"
#include "config.h"
#include "debug.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_camera.h"

static const char *TAG = "CAM";

// XIAO ESP32-S3 Sense camera pin map (OV2640).
#define CAM_PIN_PWDN   -1
#define CAM_PIN_RESET  -1
#define CAM_PIN_XCLK   10
#define CAM_PIN_SIOD   40
#define CAM_PIN_SIOC   39
#define CAM_PIN_D7     48
#define CAM_PIN_D6     11
#define CAM_PIN_D5     12
#define CAM_PIN_D4     14
#define CAM_PIN_D3     16
#define CAM_PIN_D2     18
#define CAM_PIN_D1     17
#define CAM_PIN_D0     15
#define CAM_PIN_VSYNC  38
#define CAM_PIN_HREF   47
#define CAM_PIN_PCLK   13

static bool s_inited;

static void apply_photo_settings(sensor_t *s)
{
    s->set_brightness(s, 1);
    s->set_contrast(s, 1);
    s->set_saturation(s, 0);
    s->set_quality(s, 10);
    s->set_special_effect(s, 0);

    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 2);       // Cloudy: fixed 6500K matrix, avoids auto-AWB green failure

    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_ae_level(s, 1);      // +1 EV: lifts foreground in high-contrast sky scenes
    s->set_aec_value(s, 450);

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)4);

    s->set_bpc(s, 0);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);

    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 0);
    s->set_colorbar(s, 0);
    ESP_LOGI(TAG, "applied PHOTO sensor settings");
}

static void apply_lightcheck_settings(sensor_t *s)
{
    s->set_brightness(s, 1);
    s->set_contrast(s, 0);
    s->set_quality(s, 90);
    s->set_saturation(s, 0);
    s->set_special_effect(s, 0);

    s->set_whitebal(s, 0);
    s->set_awb_gain(s, 0);

    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_ae_level(s, 0);
    s->set_aec_value(s, 500);

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)4);

    s->set_bpc(s, 0);
    s->set_wpc(s, 0);
    s->set_raw_gma(s, 0);
    s->set_lenc(s, 0);

    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 0);
    s->set_colorbar(s, 0);
    ESP_LOGI(TAG, "applied LIGHTCHECK sensor settings");
}

esp_err_t bw_cam_init(bw_cam_mode_t mode)
{
    if (s_inited) {
        ESP_LOGW(TAG, "already initialised — deinit first");
        return ESP_ERR_INVALID_STATE;
    }

    pixformat_t fmt   = (mode == BW_CAM_MODE_PHOTO) ? PIXFORMAT_JPEG
                                                    : PIXFORMAT_GRAYSCALE;
    // SXGA (1280x960): 2.56x more pixels than SVGA, comfortable memory budget.
    // Driver allocates fb_size = w*h/5 per buffer in PSRAM (JPEG AUTO mode):
    //   2 FBs = 480 KB, +copy buffer = 720 KB total — well within 8 MB PSRAM.
    // UXGA (1600x1200) is available but the buffer ceiling (375 KB) is too
    // close to what a dense outdoor scene can produce at quality 10.
    framesize_t size  = (mode == BW_CAM_MODE_PHOTO) ? FRAMESIZE_SXGA
                                                    : FRAMESIZE_QQVGA;

    camera_config_t cfg = {
        .pin_pwdn      = CAM_PIN_PWDN,
        .pin_reset     = CAM_PIN_RESET,
        .pin_xclk      = CAM_PIN_XCLK,
        .pin_sccb_sda  = CAM_PIN_SIOD,
        .pin_sccb_scl  = CAM_PIN_SIOC,
        .pin_d7        = CAM_PIN_D7,
        .pin_d6        = CAM_PIN_D6,
        .pin_d5        = CAM_PIN_D5,
        .pin_d4        = CAM_PIN_D4,
        .pin_d3        = CAM_PIN_D3,
        .pin_d2        = CAM_PIN_D2,
        .pin_d1        = CAM_PIN_D1,
        .pin_d0        = CAM_PIN_D0,
        .pin_vsync     = CAM_PIN_VSYNC,
        .pin_href      = CAM_PIN_HREF,
        .pin_pclk      = CAM_PIN_PCLK,
        .xclk_freq_hz  = 16000000,
        .ledc_timer    = LEDC_TIMER_0,
        .ledc_channel  = LEDC_CHANNEL_0,
        .pixel_format  = fmt,
        .frame_size    = size,
        .jpeg_quality  = (fmt == PIXFORMAT_JPEG) ? 12 : 0,
        .fb_count      = 2,
        .fb_location   = (fmt == PIXFORMAT_JPEG) ? CAMERA_FB_IN_PSRAM
                                                 : CAMERA_FB_IN_DRAM,
        .grab_mode     = (fmt == PIXFORMAT_JPEG) ? CAMERA_GRAB_LATEST
                                                 : CAMERA_GRAB_WHEN_EMPTY,
    };

    ESP_LOGI(TAG, "init mode=%s fmt=%d size=%d",
             mode == BW_CAM_MODE_PHOTO ? "PHOTO" : "LIGHTCHECK", fmt, size);

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) return bw_log_err(TAG, "esp_camera_init", err);

    sensor_t *s = esp_camera_sensor_get();
    if (!s) {
        ESP_LOGE(TAG, "esp_camera_sensor_get returned NULL");
        esp_camera_deinit();
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "sensor PID=0x%02x VER=0x%02x MIDH=0x%02x MIDL=0x%02x",
             s->id.PID, s->id.VER, s->id.MIDH, s->id.MIDL);

    if (mode == BW_CAM_MODE_PHOTO) apply_photo_settings(s);
    else                            apply_lightcheck_settings(s);

    // Drain frames for 500 ms while AEC/AGC converges.
    // QQVGA runs at ~70 fps (14 ms/frame); a 100 ms vTaskDelay between fb_get
    // calls leaves the 2-FB pool full for 86 ms each iteration → FB-OVF.
    // Fix: yield only 1 ms so we consume frames as fast as they arrive.
    TickType_t t0 = xTaskGetTickCount();
    while ((xTaskGetTickCount() - t0) < pdMS_TO_TICKS(500)) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
        vTaskDelay(1);
    }

    s_inited = true;
    return ESP_OK;
}

esp_err_t bw_cam_deinit(void)
{
    if (!s_inited) return ESP_OK;
    esp_err_t err = esp_camera_deinit();
    s_inited = false;
    return bw_log_err(TAG, "esp_camera_deinit", err);
}

camera_fb_t *bw_cam_capture(void)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGE(TAG, "esp_camera_fb_get returned NULL");
        return NULL;
    }
    ESP_LOGI(TAG, "captured %ux%u, %u bytes, fmt=%d",
             fb->width, fb->height, (unsigned)fb->len, fb->format);
    return fb;
}

void bw_cam_capture_return(camera_fb_t *fb)
{
    if (fb) esp_camera_fb_return(fb);
}

void bw_cam_discard_frames(int n, int delay_ms)
{
    ESP_LOGI(TAG, "discarding %d warm-up frame(s)", n);
    for (int i = 0; i < n; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
        if (delay_ms > 0) vTaskDelay(pdMS_TO_TICKS(delay_ms));
    }
}

esp_err_t bw_cam_set_format(pixformat_t fmt, framesize_t size)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) return ESP_FAIL;
    s->set_pixformat(s, fmt);
    s->set_framesize(s, size);
    vTaskDelay(pdMS_TO_TICKS(100));
    ESP_LOGI(TAG, "format switched fmt=%d size=%d", fmt, size);
    return ESP_OK;
}

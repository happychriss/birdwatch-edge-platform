#include "camera.h"
#include "config.h"
#include "debug.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_camera.h"
#include "esp_heap_caps.h"
#include <string.h>

#include "esp32s3/rom/tjpgd.h"

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
    // NORMAL mode: overcast / shaded indoor-looking-out / typical daylight
    s->set_brightness(s, 1);
    s->set_contrast(s, 1);
    s->set_saturation(s, 0);
    s->set_quality(s, 10);
    s->set_special_effect(s, 0);

    s->set_whitebal(s, 1);
    s->set_awb_gain(s, BW_CAM_AWB_GAIN);
    s->set_wb_mode(s, 2);       // Cloudy (6500K): overcast/shaded fixed matrix

    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_ae_level(s, 1);      // +1 EV: lifts foreground in high-contrast sky scenes
    s->set_aec_value(s, 450);

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)4);  // 32x

    s->set_bpc(s, 0);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);

    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 0);
    s->set_colorbar(s, 0);
    ESP_LOGI(TAG, "applied PHOTO (NORMAL) sensor settings");
}

static void apply_bright_photo_settings(sensor_t *s)
{
    // BRIGHT mode: full sun — pull back EV to protect sky, Sunny WB, lower gain
    s->set_brightness(s, 0);
    s->set_contrast(s, 2);      // higher contrast: better separation of sky vs foreground
    s->set_saturation(s, -1);   // reduce green Bayer bias amplified by bright light
    s->set_quality(s, 10);
    s->set_special_effect(s, 0);

    s->set_whitebal(s, 1);
    s->set_awb_gain(s, BW_CAM_AWB_GAIN);
    s->set_wb_mode(s, 1);       // Sunny (5500K): best for direct outdoor daylight

    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_ae_level(s, -1);     // -1 EV: prevent sky overexposure
    s->set_aec_value(s, 200);

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)2);  // 8x: plenty in full sun, less noise

    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);

    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 0);
    s->set_colorbar(s, 0);
    ESP_LOGI(TAG, "applied PHOTO_BRIGHT sensor settings");
}

static void apply_lowlight_photo_settings(sensor_t *s)
{
    s->set_brightness(s, 2);        // max brightness lift
    s->set_contrast(s, 2);          // max contrast — separates dark bird from dark foliage
    s->set_saturation(s, -1);       // reduce saturation to suppress green cast from noisy Bayer
    s->set_quality(s, 10);
    s->set_special_effect(s, 0);

    s->set_whitebal(s, 1);
    s->set_awb_gain(s, BW_CAM_AWB_GAIN);
    s->set_wb_mode(s, 2);           // Cloudy (6500K): consistent fixed matrix for dusk/dawn

    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 1);              // longer integration time: AEC stays open more frames
    s->set_ae_level(s, 2);          // +2 EV (max)
    s->set_aec_value(s, 800);

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)5);  // 32x — enough headroom, less noise than 64x

    s->set_bpc(s, 0);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);

    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 0);
    s->set_colorbar(s, 0);
    ESP_LOGI(TAG, "applied PHOTO_LOWLIGHT sensor settings");
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
    s->set_aec_value(s, 300);   // was 500 — lower starting AEC avoids noon saturation

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)2);  // 8x — was 32x which amplified noon saturation

    s->set_bpc(s, 0);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);  // gamma ON — keeps brightness scale consistent with JPEG frames
    s->set_lenc(s, 1);     // lens correction ON — keeps per-tile means consistent with JPEG

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

    bool is_photo = (mode == BW_CAM_MODE_PHOTO || mode == BW_CAM_MODE_PHOTO_LOWLIGHT || mode == BW_CAM_MODE_PHOTO_BRIGHT);
    pixformat_t fmt   = is_photo ? PIXFORMAT_JPEG      : PIXFORMAT_GRAYSCALE;
    // SXGA (1280x960): 2.56x more pixels than SVGA, comfortable memory budget.
    // Driver allocates fb_size = w*h/5 per buffer in PSRAM (JPEG AUTO mode):
    //   2 FBs = 480 KB, +copy buffer = 720 KB total — well within 8 MB PSRAM.
    // UXGA (1600x1200) is available but the buffer ceiling (375 KB) is too
    // close to what a dense outdoor scene can produce at quality 10.
    framesize_t size  = is_photo ? FRAMESIZE_SXGA      : FRAMESIZE_QQVGA;

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
        .xclk_freq_hz  = 16000000,  // 16 MHz — tested stable; 20 MHz caused FB-OVF / NO-EOI on SXGA JPEG
        .ledc_timer    = LEDC_TIMER_0,
        .ledc_channel  = LEDC_CHANNEL_0,
        .pixel_format  = fmt,
        .frame_size    = size,
        .jpeg_quality  = is_photo ? 12 : 0,
        .fb_count      = 2,
        .fb_location   = is_photo ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM,
        .grab_mode     = is_photo ? CAMERA_GRAB_LATEST : CAMERA_GRAB_WHEN_EMPTY,
    };

    ESP_LOGI(TAG, "init mode=%s fmt=%d size=%d",
             mode == BW_CAM_MODE_PHOTO          ? "PHOTO" :
             mode == BW_CAM_MODE_PHOTO_BRIGHT   ? "PHOTO_BRIGHT" :
             mode == BW_CAM_MODE_PHOTO_LOWLIGHT ? "PHOTO_LOWLIGHT" : "LIGHTCHECK",
             fmt, size);

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

    if (mode == BW_CAM_MODE_PHOTO)                apply_photo_settings(s);
    else if (mode == BW_CAM_MODE_PHOTO_BRIGHT)   apply_bright_photo_settings(s);
    else if (mode == BW_CAM_MODE_PHOTO_LOWLIGHT) apply_lowlight_photo_settings(s);
    else                                          apply_lightcheck_settings(s);

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

void bw_cam_apply_shadow_exposure(uint8_t p30)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) {
        ESP_LOGW(TAG, "shadow_exp: no sensor");
        return;
    }

    // Read AEC/AGC registers that settled during the discard-frame window.
    // init_status() reads from SCCB hardware into s->status — it is safe to
    // call after the camera is already running.
    s->init_status(s);
    uint16_t settled_aec  = s->status.aec_value;   // 0–1200
    uint8_t  settled_gain = s->status.agc_gain;    // 0–30 table index (multiplier ≈ index+1)

    if (settled_aec == 0) {
        ESP_LOGW(TAG, "shadow_exp: settled_aec=0, keeping auto");
        return;
    }

    // Shadow factor: how many times more exposure is needed so p30 reaches
    // BW_SHADOW_TARGET_DN.  Clamped ≥ 1.0 — never darken a well-exposed frame.
    float shadow_factor = 1.0f;
    if (p30 > 0 && p30 < BW_SHADOW_TARGET_DN) {
        shadow_factor = (float)BW_SHADOW_TARGET_DN / (float)p30;
        if (shadow_factor > (float)BW_SHADOW_FACTOR_MAX)
            shadow_factor = (float)BW_SHADOW_FACTOR_MAX;
    }

    // Total exposure units = aec_value × gain_multiplier.
    // Scale up, then prefer longer integration (lower noise) over higher gain.
    uint32_t E_settled = (uint32_t)settled_aec * ((uint32_t)settled_gain + 1u);
    uint32_t E_target  = (uint32_t)((float)E_settled * shadow_factor + 0.5f);

    uint16_t new_aec;
    uint8_t  new_gain;
    if (E_target <= BW_AEC_VALUE_MAX) {
        new_aec  = (uint16_t)E_target;
        new_gain = 0;   // 1× — lowest noise when integration alone is enough
    } else {
        new_aec  = BW_AEC_VALUE_MAX;
        uint32_t need = (E_target + BW_AEC_VALUE_MAX - 1u) / (uint32_t)BW_AEC_VALUE_MAX;
        new_gain = (need > 1u) ? (uint8_t)(need - 1u) : 0u;
        if (new_gain > 30u) new_gain = 30u;
    }

    s->set_exposure_ctrl(s, 0);   // switch to manual AEC
    s->set_gain_ctrl(s, 0);       // switch to manual AGC
    s->set_aec_value(s, new_aec);
    s->set_agc_gain(s, new_gain);

    // Flush the ring buffer so bw_cam_capture() returns a frame captured
    // entirely under the new manual settings.
    camera_fb_t *flush = esp_camera_fb_get();
    if (flush) esp_camera_fb_return(flush);

    ESP_LOGI(TAG, "shadow_exp: p30=%u factor=%.2f settled=%u×%u "
             "→ aec=%u gain_idx=%u (~%ux)",
             p30, (double)shadow_factor,
             settled_aec, (unsigned)(settled_gain + 1u),
             new_aec, new_gain, (unsigned)(new_gain + 1u));
}

// ── JPEG → tile YUV means (TJpgDec ROM decoder) ──────────────────────────────

typedef struct {
    uint32_t sum_r, sum_g, sum_b;
    uint32_t count;
} tile_acc_t;

typedef struct {
    const uint8_t *src;
    size_t src_len, src_pos;
    int grid_w, grid_h;
    uint16_t img_w, img_h;
    tile_acc_t *acc;
} jpeg_ctx_t;

static UINT jpeg_infunc(JDEC *jd, BYTE *buf, UINT nbytes)
{
    jpeg_ctx_t *ctx = (jpeg_ctx_t *)jd->device;
    UINT remaining = (UINT)(ctx->src_len - ctx->src_pos);
    if (nbytes > remaining) nbytes = remaining;
    if (buf) memcpy(buf, ctx->src + ctx->src_pos, nbytes);
    ctx->src_pos += nbytes;
    return nbytes;
}

static UINT jpeg_outfunc(JDEC *jd, void *bitmap, JRECT *rect)
{
    jpeg_ctx_t *ctx = (jpeg_ctx_t *)jd->device;
    const uint8_t *px = (const uint8_t *)bitmap;
    int blk_w = rect->right  - rect->left + 1;
    int blk_h = rect->bottom - rect->top  + 1;

    for (int row = 0; row < blk_h; row++) {
        for (int col = 0; col < blk_w; col++) {
            int x = rect->left + col;
            int y = rect->top  + row;
            uint8_t r = *px++, g = *px++, b = *px++;
            if (x >= ctx->img_w || y >= ctx->img_h) continue;

            int tx = x * ctx->grid_w / ctx->img_w;
            int ty = y * ctx->grid_h / ctx->img_h;
            tile_acc_t *a = &ctx->acc[ty * ctx->grid_w + tx];
            a->sum_r += r;
            a->sum_g += g;
            a->sum_b += b;
            a->count++;
        }
    }
    return 1;   // continue
}

esp_err_t bw_cam_jpeg_decode_to_tile_means(
    const uint8_t *jpeg, size_t len,
    uint8_t *tile_y, uint8_t *tile_u, uint8_t *tile_v,
    int grid_w, int grid_h)
{
    int n_tiles = grid_w * grid_h;

    tile_acc_t *acc = heap_caps_malloc((size_t)n_tiles * sizeof(tile_acc_t), MALLOC_CAP_INTERNAL);
    if (!acc) {
        ESP_LOGE(TAG, "decode: tile acc alloc failed (%d tiles)", n_tiles);
        return ESP_ERR_NO_MEM;
    }
    memset(acc, 0, (size_t)n_tiles * sizeof(tile_acc_t));

    jpeg_ctx_t ctx = {
        .src = jpeg, .src_len = len, .src_pos = 0,
        .grid_w = grid_w, .grid_h = grid_h,
        .acc = acc,
    };

    static uint8_t s_jd_pool[4096];   // TJpgDec work area — 3100 B min, 4096 B safe
    JDEC jd;
    JRESULT r = jd_prepare(&jd, jpeg_infunc, s_jd_pool, sizeof(s_jd_pool), &ctx);
    if (r != JDR_OK) {
        ESP_LOGE(TAG, "jd_prepare failed: %d", (int)r);
        free(acc);
        return ESP_FAIL;
    }

    ctx.img_w = jd.width;
    ctx.img_h = jd.height;
    ESP_LOGI(TAG, "decode %ux%u JPEG → %dx%d tiles", jd.width, jd.height, grid_w, grid_h);

    r = jd_decomp(&jd, jpeg_outfunc, 0);   // scale=0: full resolution
    if (r != JDR_OK) {
        ESP_LOGE(TAG, "jd_decomp failed: %d", (int)r);
        free(acc);
        return ESP_FAIL;
    }

    // Convert accumulated RGB sums to per-tile BT.601 YUV means.
    // Averaging RGB then converting is mathematically equivalent to
    // per-pixel conversion then averaging (BT.601 is linear).
    for (int i = 0; i < n_tiles; i++) {
        if (acc[i].count == 0) {
            tile_y[i] = tile_u[i] = tile_v[i] = 128;
            continue;
        }
        uint32_t r8 = acc[i].sum_r / acc[i].count;
        uint32_t g8 = acc[i].sum_g / acc[i].count;
        uint32_t b8 = acc[i].sum_b / acc[i].count;

        // BT.601 full-range: Y=[0,255], U/V=[0,255] centred at 128
        tile_y[i] = (uint8_t)((77u*r8 + 150u*g8 + 29u*b8) >> 8);

        int uv;
        uv = (-(int)(43*r8) - (int)(85*g8) + (int)(128*b8)) >> 8;
        uv += 128;
        tile_u[i] = (uint8_t)(uv < 0 ? 0 : uv > 255 ? 255 : uv);

        uv = ((int)(128*r8) - (int)(107*g8) - (int)(21*b8)) >> 8;
        uv += 128;
        tile_v[i] = (uint8_t)(uv < 0 ? 0 : uv > 255 ? 255 : uv);
    }

    free(acc);
    return ESP_OK;
}

#include "camera.h"
#include "config.h"
#include "cloud_check.h"   // CC_TILES_X/Y, CC_NUM_TILES for the ETTR probe decode
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
    // Single unified daylight profile.  Tone is moderate; the real exposure is set
    // afterwards by bw_cam_meter_ettr_lock(), which locks manual AEC/AGC so the
    // full-frame AEC cannot re-close on the bright sky and crush the foreground.
    s->set_brightness(s, 1);
    s->set_contrast(s, 1);
    s->set_saturation(s, 0);
    s->set_quality(s, 10);
    s->set_special_effect(s, 0);

    // AWB auto — runs during init drain + discard frames so it adapts to scene
    // colour temperature before bw_cam_awb_settle_and_lock() reads and freezes
    // the gains.  Enabling here (not mid-cycle) avoids switching AWB mode while
    // JPEG streaming is active, which corrupts the OV2640 DSP/JPEG pipeline.
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);    // auto WB — locked after settle, before ETTR

    // Auto AEC/AGC during the settle window; ETTR then locks manual exposure.
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_ae_level(s, 0);
    s->set_aec_value(s, 400);

    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)4);  // 32x headroom for the settle estimate

    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);   // keep — per-tile means stay on the same tone curve
    s->set_lenc(s, 1);      // keep — lens correction, per-tile mean consistency

    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 0);
    s->set_colorbar(s, 0);
    ESP_LOGI(TAG, "applied PHOTO (AWB auto, locks after settle) settings");
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

    bool is_photo = (mode == BW_CAM_MODE_PHOTO);
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

void bw_cam_split_exposure(uint32_t E, uint16_t *aec_out, uint8_t *gain_out)
{
    // Total exposure budget E = aec × (gain_idx + 1).  Prefer integration time
    // (lower noise); only spend gain once AEC is saturated at the ceiling.
    uint16_t aec;
    uint8_t  gain;
    if (E <= BW_AEC_VALUE_MAX) {
        aec  = (uint16_t)(E == 0 ? 1 : E);
        gain = 0;
    } else {
        aec = BW_AEC_VALUE_MAX;
        uint32_t need = (E + BW_AEC_VALUE_MAX - 1u) / (uint32_t)BW_AEC_VALUE_MAX;
        gain = (need > 1u) ? (uint8_t)(need - 1u) : 0u;
        // Cap well below the table max (30 ≈ 32×): a max-gain JPEG overflows the
        // frame buffer (FB-OVF).  Better a slightly dark frame than no frame.
        if (gain > BW_AGC_GAIN_IDX_MAX) gain = BW_AGC_GAIN_IDX_MAX;
    }
    if (aec_out)  *aec_out  = aec;
    if (gain_out) *gain_out = gain;
}

void bw_cam_set_exposure_manual(uint16_t aec, uint8_t gain)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) { ESP_LOGW(TAG, "set_exposure_manual: no sensor"); return; }
    if (aec > BW_AEC_VALUE_MAX) aec = BW_AEC_VALUE_MAX;
    if (gain > 30) gain = 30;

    s->set_exposure_ctrl(s, 0);   // manual AEC
    s->set_gain_ctrl(s, 0);       // manual AGC
    s->set_aec_value(s, aec);
    s->set_agc_gain(s, gain);

    // OV2640 exposure register changes take effect a frame or two later — flush
    // two frames so the next bw_cam_capture()/probe is fully under the new value.
    for (int i = 0; i < 2; i++) {
        camera_fb_t *flush = esp_camera_fb_get();
        if (flush) esp_camera_fb_return(flush);
    }
}

bool bw_cam_get_settled_exposure(uint16_t *aec_out, uint8_t *gain_out)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) return false;
    // init_status() reads SCCB registers into s->status — safe while running.
    s->init_status(s);
    uint16_t aec  = s->status.aec_value;   // 0–1200
    uint8_t  gain = s->status.agc_gain;    // 0–30 table index (multiplier ≈ index+1)
    if (aec == 0) return false;
    if (aec_out)  *aec_out  = aec;
    if (gain_out) *gain_out = gain;
    return true;
}

uint8_t bw_cam_hist_percentile(const uint32_t hist[256], uint32_t total, int pct)
{
    if (total == 0) return 0;
    uint32_t thresh = (uint32_t)((uint64_t)total * (uint32_t)pct / 100u);
    uint32_t cum = 0;
    for (int b = 0; b < 256; b++) {
        cum += hist[b];
        if (cum >= thresh) return (uint8_t)b;
    }
    return 255;
}

uint32_t bw_cam_hist_clip_count(const uint32_t hist[256], int from_dn)
{
    if (from_dn < 0) from_dn = 0;
    if (from_dn > 255) return 0;
    uint32_t c = 0;
    for (int b = from_dn; b < 256; b++) c += hist[b];
    return c;
}

esp_err_t bw_cam_awb_settle_and_lock(int n_frames, uint8_t *r_out, uint8_t *g_out, uint8_t *b_out)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) return ESP_FAIL;

    // AWB was enabled in apply_photo_settings() and has been running since init.
    // n_frames is ignored: settling happened during the init drain (500 ms) plus
    // bw_cam_discard_frames() in main — plenty of time without any mid-stream
    // register changes (switching AWB mode while JPEG is streaming corrupts the
    // OV2640 DSP/JPEG pipeline → FB-OVF cascade, as observed).
    (void)n_frames;

    // Read the settled per-channel gains from DSP bank 0xCC/0xCD/0xCE.
    // get_reg encodes bank in bit 8: 0x00XX = DSP bank, 0x01XX = sensor bank.
    int r = s->get_reg(s, 0x00CC, 0xFF);
    int g = s->get_reg(s, 0x00CD, 0xFF);
    int b = s->get_reg(s, 0x00CE, 0xFF);

    if (r < 0 || g < 0 || b < 0) {
        ESP_LOGW(TAG, "awb lock: readback failed (r=%d g=%d b=%d) — falling back to Sunny preset", r, g, b);
        s->set_wb_mode(s, 1);   // Sunny preset writes 0xCC/0xCD/0xCE + sets manual bit
        s->set_whitebal(s, 0);  // stop AWB algorithm
        return ESP_FAIL;
    }

    // Lock: set the manual-WB bit (0xC7 bit6) and write the settled values back
    // explicitly so they are held even if residual AWB register activity occurs.
    s->set_reg(s, 0x00C7, 0x40, 0x40);   // manual WB mode
    s->set_reg(s, 0x00CC, 0xFF, r);
    s->set_reg(s, 0x00CD, 0xFF, g);
    s->set_reg(s, 0x00CE, 0xFF, b);
    // Stop the algorithm — the locked values in 0xCC/0xCD/0xCE remain applied
    // because AWB gain (CTRL1 bit2) stays enabled.
    s->set_whitebal(s, 0);

    ESP_LOGI(TAG, "awb lock: R=0x%02x G=0x%02x B=0x%02x (after %d frame(s))",
             (unsigned)r, (unsigned)g, (unsigned)b, n_frames);

    if (r_out) *r_out = (uint8_t)r;
    if (g_out) *g_out = (uint8_t)g;
    if (b_out) *b_out = (uint8_t)b;
    return ESP_OK;
}

esp_err_t bw_cam_meter_ettr_lock(uint16_t *aec0, uint8_t *gain0)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) { ESP_LOGW(TAG, "ettr: no sensor"); return ESP_FAIL; }

    // Scratch tile arrays for the probe decode — only the histogram is consumed.
    static uint8_t ty[CC_NUM_TILES], tu[CC_NUM_TILES], tv[CC_NUM_TILES];

    uint16_t cur_aec;
    uint8_t  cur_gain;
    if (!bw_cam_get_settled_exposure(&cur_aec, &cur_gain)) {
        ESP_LOGW(TAG, "ettr: settled aec=0, keeping auto");
        return ESP_FAIL;
    }

    const float rmin = (float)BW_ETTR_RATIO_MIN_PCT / 100.0f;
    const float rmax = (float)BW_ETTR_RATIO_MAX_PCT / 100.0f;

    for (int it = 0; it < BW_ETTR_ITERS; it++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) { ESP_LOGW(TAG, "ettr: probe capture failed (it=%d)", it); goto keep_auto; }
        uint32_t hist[256] = {0};
        esp_err_t derr = bw_cam_jpeg_decode_to_tile_means(
            fb->buf, fb->len, ty, tu, tv, CC_TILES_X, CC_TILES_Y, hist);
        esp_camera_fb_return(fb);
        if (derr != ESP_OK) { ESP_LOGW(TAG, "ettr: probe decode failed (%d)", (int)derr); goto keep_auto; }

        uint32_t total = 0;
        for (int b = 0; b < 256; b++) total += hist[b];
        if (total == 0) goto keep_auto;

        // Night / no-headroom guard: if even the brightest content is dim there is
        // no highlight to expose toward — lifting would only crank gain into the
        // FB-OVF regime.  Keep the settled auto exposure (cloud-check labels NIGHT).
        uint8_t headroom = bw_cam_hist_percentile(hist, total, BW_ETTR_HEADROOM_PCT);
        if (headroom < BW_ETTR_HEADROOM_DN) {
            ESP_LOGI(TAG, "ettr: no headroom (p%d=%u < %d) — night/flat, keeping auto exposure",
                     BW_ETTR_HEADROOM_PCT, headroom, BW_ETTR_HEADROOM_DN);
            goto keep_auto;
        }

        uint8_t  p_hi    = bw_cam_hist_percentile(hist, total, BW_ETTR_HI_PERCENTILE);
        uint32_t clip    = bw_cam_hist_clip_count(hist, BW_ETTR_CLIP_DN);
        uint32_t clip_pm = (uint32_t)((uint64_t)clip * 1000u / total);

        // Exposure ratio toward the highlight target.  p_hi==0 (pitch black) →
        // push to the maximum lift.
        float ratio = (p_hi == 0) ? rmax : (float)BW_ETTR_HI_TARGET / (float)p_hi;
        // Already over the clip budget → never increase exposure this step.
        if (clip_pm > BW_ETTR_CLIP_BUDGET_PM && ratio > 1.0f) ratio = 1.0f;
        if (ratio < rmin) ratio = rmin;
        if (ratio > rmax) ratio = rmax;

        uint32_t E_cur = (uint32_t)cur_aec * ((uint32_t)cur_gain + 1u);
        uint32_t E_new = (uint32_t)((float)E_cur * ratio + 0.5f);

        uint16_t new_aec;
        uint8_t  new_gain;
        bw_cam_split_exposure(E_new, &new_aec, &new_gain);
        bw_cam_set_exposure_manual(new_aec, new_gain);

        ESP_LOGI(TAG, "ettr it=%d: p_hi=%u clip=%u%%o headroom=%u ratio=%.2f E %u->%u → aec=%u gain_idx=%u",
                 it, p_hi, (unsigned)clip_pm, headroom, (double)ratio,
                 (unsigned)E_cur, (unsigned)E_new, new_aec, new_gain);

        cur_aec = new_aec; cur_gain = new_gain;

        // Converged — a near-unity correction means no point probing again.
        if (ratio > 0.9f && ratio < 1.1f) break;
    }

    if (aec0)  *aec0  = cur_aec;
    if (gain0) *gain0 = cur_gain;
    return ESP_OK;

keep_auto:
    // Night / flat scene.  Revert WB to the fixed Sunny preset with adaptive gain
    // OFF — exactly the proven pre-AWB night state.  Auto-AWB boosts every channel
    // gain toward ~0x80 in a dark scene; stacked on the high night AGC gain this
    // amplifies sensor noise enough to overflow the 256 KB SXGA JPEG buffer
    // (FB-OVF cascade).  The fixed Sunny matrix adds no per-channel digital boost,
    // so the night frame stays small and the bracket captures succeed (as they did
    // before the AWB-lock change).  Daytime keeps the locked auto-AWB gains.
    s->set_whitebal(s, 1);   // AWB algorithm on, but…
    s->set_awb_gain(s, 0);   // …no adaptive per-channel digital gain
    s->set_wb_mode(s, 1);    // fixed Sunny preset (5500K daylight)
    // Restore auto AEC/AGC so the caller falls back to a known-good exposure
    // (the settled auto exposure yields a valid frame even when ETTR bails).
    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
    return ESP_FAIL;
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
    uint32_t   *hist;   // optional 256-bin luma histogram (NULL to skip)
    uint32_t   blocks;  // MCU-block counter — drives the periodic idle-task yield
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

            if (ctx->hist) {
                // BT.601 luma — same weights as the per-tile Y below.
                uint8_t luma = (uint8_t)((77u * r + 150u * g + 29u * b) >> 8);
                ctx->hist[luma]++;
            }
        }
    }
    // Full-res SXGA decode is a ~2.7 s tight CPU loop with no blocking call, which
    // starves the IDLE0 task and trips the Task Watchdog (TWDT, 5 s).  Yield one
    // tick every 1024 MCU blocks (≈7 yields per frame) so IDLE0 runs and the TWDT
    // stays fed.  Costs ~tens of ms total; tile means / histogram are unaffected.
    if ((++ctx->blocks & 0x3FFu) == 0) vTaskDelay(1);
    return 1;   // continue
}

esp_err_t bw_cam_jpeg_decode_to_tile_means(
    const uint8_t *jpeg, size_t len,
    uint8_t *tile_y, uint8_t *tile_u, uint8_t *tile_v,
    int grid_w, int grid_h, uint32_t *hist256)
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
        .hist = hist256,
        .blocks = 0,
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

// ─── Cloud-check filter — ESP32-S3 port of the Python pipeline ───────────────
//
// Algorithm matches src/cloud-check/ exactly.  Decision stages (priority order):
//
//   NIGHT       — frame too dark for reliable detection → upload (sun is down)
//   WARMUP      — model not yet bootstrapped; upload always (can't risk missing a bird)
//   DARK_OBJ    — tiles newly dark vs both model AND previous frame → real object → upload
//   QUIET       — ≤25 % dark-anomalous tiles → scene matches model → suppress
//   SCENE_DRIFT — tiles dark vs model but NOT newly dark vs prev → stale model → upload + re-calibrate
//   AMBIGUOUS   — default → upload
//
// QUIET uses dark-only anomaly ratio: only tiles DARKER than the model count.
// Bright deviations (sky brightening, cloud moving off sun) are ignored so PIR
// triggers caused by illumination increase don't prevent suppression.
//
// Uses QQVGA (160×120) grayscale, 20×15 tile grid (8×8 px per tile, 300 tiles total).
// Background model (mean + variance per tile) and previous-frame tile means persist in NVS
// under namespace "cc" so state survives power-off between PIR events.
//
// All thresholds and behaviour are controlled by the #define constants below.
// To tune: change the constant, rebuild, flash.

#include "cloud_check.h"
#include "camera.h"
#include "debug.h"
#include "telemetry.h"

#include <string.h>
#include <math.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>

#include "esp_log.h"
#include "nvs.h"

static const char *TAG    = "CC";
static const char *NVS_NS = "cc";

// ── Frame / grid layout ──────────────────────────────────────────────────────
// QQVGA: 160×120.  20×15 grid gives 8×8 px tiles (300 tiles total).
// Smaller tiles improve detection of small/distant birds vs the old 16×12 grid.
#define CC_FRAME_W    160
#define CC_FRAME_H    120
#define CC_TILES_X    20
#define CC_TILES_Y    15
#define CC_NUM_TILES  (CC_TILES_X * CC_TILES_Y)     // 300
#define CC_TILE_W     (CC_FRAME_W / CC_TILES_X)     // 8
#define CC_TILE_H     (CC_FRAME_H / CC_TILES_Y)     // 8

// ── Background model parameters ──────────────────────────────────────────────
// Values found by parameter sweep over 195-frame real-scene dataset.
// nc_recall=1.000, c_recall=0.618 (20×15, dark-only QUIET).
#define CC_EMA_ALPHA        0.15f   // background update speed (lower = slower adaptation)
#define CC_VAR_FLOOR        36.0f   // minimum tile variance (std ≥ 6); prevents over-confidence
#define CC_INIT_VAR         256.0f  // variance prior for unseen tiles (std = 16)
#define CC_INIT_MEAN        128.0f  // mean prior for unseen tiles (mid-scale grey)
#define CC_Z_THRESHOLD      3.0f    // z-score to flag a tile as anomalous
#define CC_QUIET_RATIO      0.25f   // ≤25 % dark-anomalous tiles → QUIET → suppress
#define CC_DARK_DELTA_MODEL 35.0f   // tile must be ≥35 DN darker than model mean (DARK_OBJ check)
#define CC_DARK_DELTA_PREV  20.0f   // tile must be ≥20 DN darker than previous frame (temporal check)
#define CC_DARK_MIN_TILES        1  // ≥1 qualifying dark tile triggers DARK_OBJ
#define CC_SCENE_DRIFT_MIN_TILES 4  // SCENE_DRIFT needs ≥4 persistently-dark tiles
#define CC_WARMUP_FRAMES    4       // frames before model is considered bootstrapped
#define CC_NIGHT_THRESHOLD  70      // frame global mean below this → NIGHT → upload (sun is down)

// ── Burst pre-filter parameters ───────────────────────────────────────────────
// Matches BurstConfig() defaults in burst_filter.py exactly.
// Applied BEFORE the background-model pipeline to suppress PIR re-fires on
// cloud/sun transitions.  Does NOT implement FAST_SHIFT or ISOLATED — those
// stages require dt_seconds which is unavailable before WiFi/SNTP sync.
// They are validated offline against training data by validate_burst.py.
#define CC_BURST_BRIGHT_SIM_THR   12    // |gm_diff| > this → BRIGHTNESS_SHIFT → process
#define CC_BURST_TILE_DIFF_THR    12    // |tile_diff| > this → counts toward n_changed
#define CC_BURST_DARK_DIFF_THR    12    // (prev - curr) > this → counts toward n_dark
#define CC_BURST_DUP_MAX_TILES     0    // n_changed ≤ this → DUPLICATE → suppress
#define CC_BURST_DIFFUSE_MIN_DARK 60    // n_dark ≥ this → DIFFUSE → suppress
#define CC_BURST_BS_MIN_GM       160    // BRIGHT_STABLE: global_mean must exceed this
#define CC_BURST_BS_MAX_DARK      35    // BRIGHT_STABLE: suppress when n_dark < this

// ── NVS key names ─────────────────────────────────────────────────────────────
// "cc_m"   : tile means     (300 × float  = 1200 B)
// "cc_v"   : tile variances (300 × float  = 1200 B)
// "cc_p"   : previous-frame tile means (300 × uint8 = 300 B)
// "cc_pgm" : previous-frame global mean (uint8)
// "cc_seen": total frames observed this bucket (uint16)
// NOTE: blob size change (192→300 tiles) is detected on load — mismatched blobs
// are silently discarded and the model re-initialises from the prior.
static const char *KEY_MEAN    = "cc_m";
static const char *KEY_VAR     = "cc_v";
static const char *KEY_PREV    = "cc_p";
static const char *KEY_PREV_GM = "cc_pgm";
static const char *KEY_SEEN    = "cc_seen";

// ── In-RAM model state ────────────────────────────────────────────────────────
static float    s_mean[CC_NUM_TILES];
static float    s_var[CC_NUM_TILES];
static uint8_t  s_prev[CC_NUM_TILES];
static bool     s_prev_valid    = false;
static uint8_t  s_prev_gm       = 128;
static bool     s_prev_gm_valid = false;
static uint16_t s_frames_seen   = 0;   // total non-NIGHT frames processed (mirrors Python bucket_seen
                                      // for the active time-of-day bucket; NIGHT frames skip this
                                      // counter because they precede the NIGHT gate and updating the
                                      // count there would over-inflate warmup — same net effect as
                                      // Python's per-bucket counters where NIGHT frames go into
                                      // bucket 0 / the night bucket, not the active daytime bucket)

// ── NVS helpers ───────────────────────────────────────────────────────────────

static void load_model(void)
{
    nvs_handle_t h;
    bool ok = false;

    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        size_t sm = sizeof(s_mean), sv = sizeof(s_var);
        bool got_mean = (nvs_get_blob(h, KEY_MEAN, s_mean, &sm) == ESP_OK && sm == sizeof(s_mean));
        bool got_var  = (nvs_get_blob(h, KEY_VAR,  s_var,  &sv) == ESP_OK && sv == sizeof(s_var));
        uint16_t seen = 0;
        nvs_get_u16(h, KEY_SEEN, &seen);
        s_frames_seen = seen;
        nvs_close(h);
        ok = got_mean && got_var;
    }

    if (!ok) {
        for (int i = 0; i < CC_NUM_TILES; i++) {
            s_mean[i] = CC_INIT_MEAN;
            s_var[i]  = CC_INIT_VAR;
        }
        s_frames_seen = 0;
        ESP_LOGI(TAG, "no prior model — initialised fresh");
    }
}

static void save_model(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "model save: nvs_open failed");
        return;
    }
    nvs_set_blob(h, KEY_MEAN, s_mean, sizeof(s_mean));
    nvs_set_blob(h, KEY_VAR,  s_var,  sizeof(s_var));
    nvs_set_u16(h, KEY_SEEN, s_frames_seen);
    nvs_commit(h);
    nvs_close(h);
}

static void load_prev(void)
{
    nvs_handle_t h;
    s_prev_valid    = false;
    s_prev_gm_valid = false;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    size_t sp = sizeof(s_prev);
    s_prev_valid = (nvs_get_blob(h, KEY_PREV, s_prev, &sp) == ESP_OK && sp == sizeof(s_prev));
    uint8_t pgm = 128;
    s_prev_gm_valid = (nvs_get_u8(h, KEY_PREV_GM, &pgm) == ESP_OK);
    s_prev_gm = pgm;
    nvs_close(h);
    if (!s_prev_valid) ESP_LOGI(TAG, "no prior frame — burst+temporal check skipped");
}

static void save_prev(const uint8_t *tile_means, uint8_t global_mean)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "prev save: nvs_open failed");
        return;
    }
    nvs_set_blob(h, KEY_PREV, tile_means, CC_NUM_TILES);
    nvs_set_u8(h,  KEY_PREV_GM, global_mean);
    nvs_commit(h);
    nvs_close(h);
}

// ── Feature extraction ────────────────────────────────────────────────────────

static void extract_tile_means(const uint8_t *frame, uint8_t *out)
{
    for (int ty = 0; ty < CC_TILES_Y; ty++) {
        for (int tx = 0; tx < CC_TILES_X; tx++) {
            uint32_t sum = 0;
            for (int py = 0; py < CC_TILE_H; py++) {
                const uint8_t *row = frame + (ty * CC_TILE_H + py) * CC_FRAME_W + tx * CC_TILE_W;
                for (int px = 0; px < CC_TILE_W; px++) sum += row[px];
            }
            out[ty * CC_TILES_X + tx] = (uint8_t)(sum / (CC_TILE_W * CC_TILE_H));
        }
    }
}

// ── EMA background update ─────────────────────────────────────────────────────
// Matches background.py BackgroundModel.update() exactly:
//   new_mean = (1-α)*mean + α*x
//   residual = x - new_mean          (= delta * (1-α))
//   new_var  = (1-α)*var + α*residual²

static void update_model(const uint8_t *means)
{
    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x        = (float)means[i];
        float new_mean = (1.0f - CC_EMA_ALPHA) * s_mean[i] + CC_EMA_ALPHA * x;
        float residual = x - new_mean;
        float new_var  = (1.0f - CC_EMA_ALPHA) * s_var[i] + CC_EMA_ALPHA * residual * residual;
        s_mean[i] = new_mean;
        s_var[i]  = new_var < CC_VAR_FLOOR ? CC_VAR_FLOOR : new_var;
    }
}

// ── Decision pipeline ─────────────────────────────────────────────────────────
// Matches classifier.py classify() + pipeline.py run_stream() exactly.
//
// Key facts:
// 1. z-score is ABSOLUTE (both brighter AND darker tiles are anomalous).
//    bright anomalies don't trigger dark_obj but DO increase the QUIET ratio.
// 2. dark_model_tiles and new_dark_tiles are counted independently — they can
//    be DIFFERENT tiles. DARK_OBJ fires when ≥1 of each exists (not the same tile).
// 3. s_frames_seen is incremented BEFORE the warmup check (mirrors Python's
//    model.observe() call before classify()).

static void run_pipeline(const uint8_t *means, bw_cc_result_t *out)
{
    uint32_t gm_sum = 0;
    for (int i = 0; i < CC_NUM_TILES; i++) gm_sum += means[i];
    uint32_t global_mean = gm_sum / CC_NUM_TILES;
    out->global_mean = (uint8_t)global_mean;
    bw_tele_i("global_mean", (long)global_mean);
    bw_tele_arr_u8("tile_means", means, CC_NUM_TILES);

    // Background model means (pre-update snapshot) — lets the server render Δm per tile.
    // s_mean[] was loaded from NVS before run_pipeline(); this is the state that z-scores
    // are computed from.  Rounded to uint8 for compact JSON.
    {
        uint8_t model_m[CC_NUM_TILES];
        for (int i = 0; i < CC_NUM_TILES; i++) {
            float v = s_mean[i] + 0.5f;
            model_m[i] = (v < 0.0f) ? 0u : (v > 255.0f) ? 255u : (uint8_t)v;
        }
        bw_tele_arr_u8("model_tile_means", model_m, CC_NUM_TILES);
    }

    // ── Burst pre-filter ──────────────────────────────────────────────────────
    // Compares current frame to the previous captured frame to suppress PIR
    // re-fires on sun/cloud transitions.  The following stages match Python
    // burst_filter.py exactly for the cases that don't require dt_seconds.
    // FAST_SHIFT and ISOLATED are omitted — no wall clock before WiFi/SNTP.
    {
        const char *burst_trigger;
        const char *burst_label;
        int gm_diff    = 0;
        int n_changed  = 0;
        int n_dark     = 0;

        if (!s_prev_valid || !s_prev_gm_valid) {
            // FIRST — no previous frame available
            burst_trigger = "FIRST";
            burst_label   = "process";
        } else {
            // Signed diff, then abs — use int to avoid uint8 wrap-around
            gm_diff = (int)global_mean - (int)s_prev_gm;
            if (gm_diff < 0) gm_diff = -gm_diff;

            if (gm_diff > CC_BURST_BRIGHT_SIM_THR) {
                // BRIGHTNESS_SHIFT — large whole-scene illumination change → process.
                // Covers the role of ISOLATED + FAST_SHIFT + BRIGHTNESS_SHIFT for the
                // no-dt case: any significant brightness jump is treated as process.
                burst_trigger = "BRIGHTNESS_SHIFT";
                burst_label   = "process";
            } else {
                // Compute tile-level diffs (only when gm_diff is within threshold)
                for (int i = 0; i < CC_NUM_TILES; i++) {
                    int diff = (int)means[i] - (int)s_prev[i];
                    if (diff < 0 ? -diff > CC_BURST_TILE_DIFF_THR : diff > CC_BURST_TILE_DIFF_THR)
                        n_changed++;
                    if (-diff > CC_BURST_DARK_DIFF_THR)   // prev - curr > thr: got darker
                        n_dark++;
                }

                if (n_changed <= CC_BURST_DUP_MAX_TILES) {
                    // DUPLICATE — pixel-identical burst re-fire
                    burst_trigger = "DUPLICATE";
                    burst_label   = "suppress";
                } else if ((uint32_t)global_mean > CC_BURST_BS_MIN_GM
                           && n_dark < CC_BURST_BS_MAX_DARK) {
                    // BRIGHT_STABLE — bright scene, very few dark tiles: no object present
                    burst_trigger = "BRIGHT_STABLE";
                    burst_label   = "suppress";
                } else if (n_dark >= CC_BURST_DIFFUSE_MIN_DARK) {
                    // DIFFUSE — massive darkening: cloud shadow sweeping the scene
                    burst_trigger = "DIFFUSE";
                    burst_label   = "suppress";
                } else {
                    // SAFE — safety bias: upload
                    burst_trigger = "SAFE";
                    burst_label   = "process";
                }
            }
        }

        bw_tele_s("burst_trigger",   burst_trigger);
        bw_tele_s("burst_label",     burst_label);
        bw_tele_i("burst_gm_diff",   gm_diff);
        bw_tele_i("burst_n_changed", n_changed);
        bw_tele_i("burst_n_dark",    n_dark);
        ESP_LOGI(TAG, "BURST %-17s gm_diff=%d n=%d nd=%d → %s",
                 burst_trigger, gm_diff, n_changed, n_dark, burst_label);

        if (burst_label[0] == 's') {   // "suppress"
            save_prev(means, (uint8_t)global_mean);
            strcpy(out->label, "clouds");
            strcpy(out->stage, burst_trigger);
            bw_tele_s("result", "clouds");
            bw_tele_s("stage",  burst_trigger);
            return;
        }
        // burst_label == "process" → fall through to background-model pipeline
    }

    // ── NIGHT ─────────────────────────────────────────────────────────────────
    // Frame too dark for reliable anomaly detection → upload unconditionally.
    // Proxy for "sun is down" that requires no clock or location.  Matches Python
    // Stage 0 in classifier.py (tile_mean.mean() < night_brightness_threshold).
    if (global_mean < CC_NIGHT_THRESHOLD) {
        update_model(means);
        save_model();
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "NIGHT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "NIGHT");
        ESP_LOGI(TAG, "NIGHT (global_mean=%" PRIu32 " < %d) → process", global_mean, CC_NIGHT_THRESHOLD);
        return;
    }

    // Mirror Python's model.observe(): increment for every non-NIGHT frame,
    // BEFORE the warmup check.  NIGHT frames are excluded (they return above)
    // for the same reason Python's per-bucket counter is unaffected by NIGHT:
    // a frame too dark to make a reliable cloud/object call should not advance
    // the bucket toward confident-suppression mode.
    if (s_frames_seen < 0xFFFF) s_frames_seen++;

    // WARMUP — model not yet bootstrapped
    bool warmup = (s_frames_seen < CC_WARMUP_FRAMES);
    bw_tele_b("warmup", warmup);
    if (warmup) {
        update_model(means);
        save_model();
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "WARMUP");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "WARMUP");
        ESP_LOGI(TAG, "WARMUP (%u frames seen) → process", s_frames_seen);
        return;
    }

    // Per-tile anomaly analysis
    // z = |model_mean - tile_mean| / std
    // dark_anomalous  : z > threshold AND tile darker than model (used for QUIET ratio)
    // dark_model_tiles: z > threshold AND tile ≥ CC_DARK_DELTA_MODEL darker than model (DARK_OBJ)
    // new_dark_tiles  : z > threshold AND tile ≥ CC_DARK_DELTA_PREV darker than prev frame (DARK_OBJ)
    // QUIET uses dark-only ratio: bright deviations (sky brightening) are ignored.
    int dark_anomalous   = 0;
    int dark_model_tiles = 0;
    int new_dark_tiles   = 0;

    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x   = (float)means[i];
        float m   = s_mean[i];
        float std = sqrtf(s_var[i]);

        float z = fabsf(m - x) / std;
        bool z_anom = (z > CC_Z_THRESHOLD);

        if (z_anom) {
            if (x < m) dark_anomalous++;   // only darker-than-model tiles count toward QUIET ratio
            if (m - x > CC_DARK_DELTA_MODEL) dark_model_tiles++;
            if (s_prev_valid && (float)s_prev[i] - x > CC_DARK_DELTA_PREV) new_dark_tiles++;
        }
    }

    float ratio = (float)dark_anomalous / CC_NUM_TILES;
    bw_tele_i("dark_anomalous", (long)dark_anomalous);
    bw_tele_f("ratio",          (double)ratio);
    bw_tele_i("dark_tiles",     (long)dark_model_tiles);
    bw_tele_i("new_dark_tiles", (long)new_dark_tiles);
    bw_tele_b("prev_valid",     s_prev_valid);

    // dark_obj_condition matches Python exactly:
    //   dark_tiles >= 1 AND (no temporal OR new_dark_tiles >= 1)
    bool dark_obj_cond = (dark_model_tiles >= CC_DARK_MIN_TILES) &&
                         (!s_prev_valid || new_dark_tiles >= CC_DARK_MIN_TILES);

    // stale_condition: many tiles persistently dark (≥ CC_SCENE_DRIFT_MIN_TILES) but none newly dark.
    // Higher threshold than DARK_OBJ — requires a bigger scene change to call it a drift.
    bool stale_cond = (dark_model_tiles >= CC_SCENE_DRIFT_MIN_TILES) &&
                      s_prev_valid && (new_dark_tiles < CC_DARK_MIN_TILES);

    // DARK_OBJ — compact dark object appeared this frame
    if (dark_obj_cond) {
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "DARK_OBJ");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "DARK_OBJ");
        ESP_LOGI(TAG, "DARK_OBJ (dark_model=%d new_dark=%d ratio=%.0f%%) → process",
                 dark_model_tiles, new_dark_tiles, ratio * 100.0f);
        return;
    }

    // QUIET — dark-anomalous ratio low → scene matches model → suppress
    if (ratio <= CC_QUIET_RATIO) {
        update_model(means);
        save_model();
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "clouds");
        strcpy(out->stage, "QUIET");
        bw_tele_s("result", "clouds");
        bw_tele_s("stage",  "QUIET");
        ESP_LOGI(TAG, "QUIET (ratio=%.0f%%) → clouds (suppress)", ratio * 100.0f);
        return;
    }

    // SCENE_DRIFT — tiles dark vs model were already present in previous frame
    // → model is stale (overnight scene change); re-calibrate and upload to be safe
    if (stale_cond) {
        update_model(means);
        s_frames_seen = 0;   // reset warmup — scene changed, re-bootstrap before suppressing
        save_model();        // saves s_frames_seen = 0 via KEY_SEEN
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "SCENE_DRIFT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "SCENE_DRIFT");
        ESP_LOGI(TAG, "SCENE_DRIFT (dark_model=%d new_dark=0) → process + warmup reset",
                 dark_model_tiles);
        return;
    }

    // AMBIGUOUS — default: upload (safety bias, never suppress on doubt)
    save_prev(means, (uint8_t)global_mean);
    strcpy(out->label, "process");
    strcpy(out->stage, "AMBIGUOUS");
    bw_tele_s("result", "process");
    bw_tele_s("stage",  "AMBIGUOUS");
    ESP_LOGI(TAG, "AMBIGUOUS (dark_ratio=%.0f%% dark_model=%d new_dark=%d) → process",
             ratio * 100.0f, dark_model_tiles, new_dark_tiles);
}

// ── Public API ────────────────────────────────────────────────────────────────

void bw_cc_reset(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "cc_reset: nvs_open failed");
        return;
    }
    nvs_erase_key(h, KEY_MEAN);
    nvs_erase_key(h, KEY_VAR);
    nvs_erase_key(h, KEY_PREV);
    nvs_erase_key(h, KEY_PREV_GM);
    nvs_erase_key(h, KEY_SEEN);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "background model cleared (firmware update)");
}

esp_err_t bw_cc_assess(bw_cc_result_t *out)
{
    memset(out, 0, sizeof(*out));
    bw_tele_reset();   // fresh telemetry object for this capture cycle

    esp_err_t err = bw_cam_init(BW_CAM_MODE_LIGHTCHECK);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed: %s", esp_err_to_name(err));
        strcpy(out->label, "process");
        strcpy(out->stage, "CAM_ERR");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "CAM_ERR");
        return err;
    }

    camera_fb_t *fb = bw_cam_capture();
    if (!fb || fb->len < (size_t)(CC_FRAME_W * CC_FRAME_H)) {
        ESP_LOGE(TAG, "no valid frame (len=%u)", fb ? (unsigned)fb->len : 0u);
        if (fb) bw_cam_capture_return(fb);
        bw_cam_deinit();
        strcpy(out->label, "process");
        strcpy(out->stage, "CAM_ERR");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "CAM_ERR");
        return ESP_FAIL;
    }

    uint8_t means[CC_NUM_TILES];
    extract_tile_means(fb->buf, means);
    bw_cam_capture_return(fb);
    bw_cam_deinit();

    load_model();
    load_prev();
    run_pipeline(means, out);

    ESP_LOGI(TAG, "─── result: %-9s  stage: %-11s  seen: %u  prev: %s ───",
             out->label, out->stage, s_frames_seen,
             s_prev_valid ? "yes" : "no");

    return ESP_OK;
}

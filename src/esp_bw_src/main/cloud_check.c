// ─── Cloud-check filter — ESP32-S3 port of the Python pipeline ───────────────
//
// Algorithm matches src/cloud-check/ exactly.  Decision stages (priority order):
//
//   NIGHT          — frame too dark for reliable detection → upload (sun is down)
//   WARMUP         — model not yet bootstrapped; upload always (can't risk missing a bird)
//   DARK_OBJ       — tiles newly dark vs both model AND previous frame → real object → upload
//   INDIRECT_LIGHT — low-to-moderate brightness + high contrast (sun from side); z-scores
//                    unreliable → admit limitation, upload unconditionally
//   QUIET          — ≤20 % tiles anomalous → scene matches model → suppress
//   SCENE_DRIFT    — tiles dark vs model but NOT newly dark vs prev → stale model → upload + re-calibrate
//   AMBIGUOUS      — default → upload
//
// Uses QQVGA (160×120) grayscale, 16×12 tile grid (10×10 px per tile, 192 tiles total).
// Background model (mean + variance per tile) and previous-frame tile means persist in NVS
// under namespace "cc" so state survives power-off between PIR events.
//
// All thresholds and behaviour are controlled by the #define constants below.
// To tune: change the constant, rebuild, flash.

#include "cloud_check.h"
#include "camera.h"
#include "debug.h"

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
// QQVGA: 160×120.  16×12 grid gives 10×10 px tiles — same tile count (192) as
// the Python simulation running on 640×480 with 40×40 tiles.
#define CC_FRAME_W    160
#define CC_FRAME_H    120
#define CC_TILES_X    16
#define CC_TILES_Y    12
#define CC_NUM_TILES  (CC_TILES_X * CC_TILES_Y)     // 192
#define CC_TILE_W     (CC_FRAME_W / CC_TILES_X)     // 10
#define CC_TILE_H     (CC_FRAME_H / CC_TILES_Y)     // 10

// ── Background model parameters ──────────────────────────────────────────────
// Values found by focused grid search (5184 configurations) over the
// 172-frame real-scene dataset.  Non-cloud recall = 1.000, cloud recall = 0.516.
#define CC_EMA_ALPHA        0.15f   // background update speed (lower = slower adaptation)
#define CC_VAR_FLOOR        36.0f   // minimum tile variance (std ≥ 6); prevents over-confidence
#define CC_INIT_VAR         256.0f  // variance prior for unseen tiles (std = 16)
#define CC_INIT_MEAN        128.0f  // mean prior for unseen tiles (mid-scale grey)
#define CC_Z_THRESHOLD      2.5f    // z-score to flag a tile as anomalous (both bright AND dark)
#define CC_QUIET_RATIO      0.20f   // ≤20 % anomalous → QUIET → suppress
#define CC_DARK_DELTA_MODEL 35.0f   // tile must be ≥35 DN darker than model mean (DARK_OBJ check)
#define CC_DARK_DELTA_PREV  20.0f   // tile must be ≥20 DN darker than previous frame (temporal check)
#define CC_DARK_MIN_TILES        1  // ≥1 qualifying dark tile triggers DARK_OBJ
#define CC_SCENE_DRIFT_MIN_TILES 4  // SCENE_DRIFT needs ≥4 persistently-dark tiles (bigger scene change)
#define CC_WARMUP_FRAMES    8       // frames before model is considered bootstrapped
#define CC_NIGHT_THRESHOLD      70  // frame global mean below this → NIGHT → upload (sun is down)
#define CC_INDIRECT_THRESHOLD   95  // global mean in (NIGHT, INDIRECT) → indirect light → upload
#define CC_SPOT_MAX_TILES    2      // SPOT_CHANGE: max tiles darkened vs prev (set 0 to disable)
#define CC_SPOT_TILE_DELTA   15.0f  // SPOT_CHANGE: tile must darken this much vs prev frame
#define CC_SPOT_GLOBAL_STAB  10.0f  // SPOT_CHANGE: max allowed global_mean shift vs prev frame
#define CC_SPOT_MAX_NOISY    20     // SPOT_CHANGE: max tiles with |any| change ≥10 DN

// ── NVS key names ─────────────────────────────────────────────────────────────
// "cc_m"   : tile means     (192 × float  = 768 B)
// "cc_v"   : tile variances (192 × float  = 768 B)
// "cc_p"   : previous-frame tile means (192 × uint8 = 192 B)
// "cc_seen": total frames observed this bucket (uint16)
static const char *KEY_MEAN  = "cc_m";
static const char *KEY_VAR   = "cc_v";
static const char *KEY_PREV  = "cc_p";
static const char *KEY_SEEN  = "cc_seen";

// ── In-RAM model state ────────────────────────────────────────────────────────
static float    s_mean[CC_NUM_TILES];
static float    s_var[CC_NUM_TILES];
static uint8_t  s_prev[CC_NUM_TILES];
static bool     s_prev_valid  = false;
static uint16_t s_frames_seen = 0;   // total non-NIGHT frames processed (mirrors Python bucket_seen
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
    s_prev_valid = false;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    size_t sp = sizeof(s_prev);
    s_prev_valid = (nvs_get_blob(h, KEY_PREV, s_prev, &sp) == ESP_OK && sp == sizeof(s_prev));
    nvs_close(h);
    if (!s_prev_valid) ESP_LOGI(TAG, "no prior frame — temporal check skipped");
}

static void save_prev(const uint8_t *tile_means)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "prev save: nvs_open failed");
        return;
    }
    nvs_set_blob(h, KEY_PREV, tile_means, CC_NUM_TILES);
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
    // NIGHT — frame too dark for reliable anomaly detection → upload unconditionally.
    // Proxy for "sun is down" that requires no clock or location.  Matches Python
    // Stage 0 in classifier.py (tile_mean.mean() < night_brightness_threshold).
    uint32_t gm_sum = 0;
    for (int i = 0; i < CC_NUM_TILES; i++) gm_sum += means[i];
    uint32_t global_mean = gm_sum / CC_NUM_TILES;
    out->global_mean = (uint8_t)global_mean;
    if (global_mean < CC_NIGHT_THRESHOLD) {
        update_model(means);
        save_model();
        save_prev(means);
        strcpy(out->label, "process");
        strcpy(out->stage, "NIGHT");
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
    if (warmup) {
        update_model(means);
        save_model();
        save_prev(means);
        strcpy(out->label, "process");
        strcpy(out->stage, "WARMUP");
        ESP_LOGI(TAG, "WARMUP (%u frames seen) → process", s_frames_seen);
        return;
    }

    // Per-tile anomaly analysis
    // z = |model_mean - tile_mean| / std  (absolute — matches Python np.abs())
    // dark_model_tiles: anomalous AND darker than model by ≥ CC_DARK_DELTA_MODEL
    // new_dark_tiles  : anomalous AND darker than previous frame by ≥ CC_DARK_DELTA_PREV
    // These two sets are independent (a tile can be in one but not the other).
    int anomalous        = 0;
    int dark_model_tiles = 0;
    int new_dark_tiles   = 0;

    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x   = (float)means[i];
        float m   = s_mean[i];
        float std = sqrtf(s_var[i]);

        // Absolute z-score: bright AND dark deviations both count as anomalous.
        float z = fabsf(m - x) / std;
        bool z_anom = (z > CC_Z_THRESHOLD);

        if (z_anom) {
            anomalous++;
            // Strictly more than threshold — mirrors Python's (delta < -N) which excludes exactly N.
            if (m - x > CC_DARK_DELTA_MODEL) dark_model_tiles++;
            if (s_prev_valid && (float)s_prev[i] - x > CC_DARK_DELTA_PREV) new_dark_tiles++;
        }
    }

    float ratio = (float)anomalous / CC_NUM_TILES;

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
        save_prev(means);
        strcpy(out->label, "process");
        strcpy(out->stage, "DARK_OBJ");
        ESP_LOGI(TAG, "DARK_OBJ (dark_model=%d new_dark=%d ratio=%.0f%%) → process",
                 dark_model_tiles, new_dark_tiles, ratio * 100.0f);
        return;
    }

    // INDIRECT_LIGHT — low-to-moderate brightness with high spatial contrast.
    // Low-angle sun (morning/evening) creates hard directional shadows that cause
    // PIR false triggers.  In this brightness zone the background model accumulates
    // high variance from sun/cloud cycling, making z-scores unreliable: even a
    // 100 DN object delta can produce z < 2.5.  We cannot distinguish a cloud
    // shadow from a small dark object, so we admit the limitation and upload.
    // Model is updated so it tracks the indirect-light baseline.
    if (global_mean < CC_INDIRECT_THRESHOLD) {
        update_model(means);
        save_model();
        save_prev(means);
        strcpy(out->label, "process");
        strcpy(out->stage, "INDIRECT_LIGHT");
        ESP_LOGI(TAG, "INDIRECT_LIGHT (global_mean=%" PRIu32 " < %d) → process",
                 global_mean, CC_INDIRECT_THRESHOLD);
        return;
    }

    // SPOT_CHANGE — scene globally stable vs previous frame but exactly 1..CC_SPOT_MAX_TILES
    // tiles darkened significantly.  Safety net for small objects (distant bird, partial
    // view) whose per-tile z-score vs the background model is below CC_Z_THRESHOLD, so
    // DARK_OBJ misses them.  Also guards against the case where the model is stale for
    // a particular tile (recent scene change) but the object is clearly new vs prev.
    // Requires: prev frame available, global_mean stable, few dark spots, low overall churn.
    if (CC_SPOT_MAX_TILES > 0 && s_prev_valid) {
        // Use float division for both sides so g_delta precision matches Python.
        // global_mean (uint32) was integer-divided; recompute as float here.
        float cur_gm_f = (float)gm_sum / CC_NUM_TILES;
        uint32_t prev_gm_sum = 0;
        for (int i = 0; i < CC_NUM_TILES; i++) prev_gm_sum += s_prev[i];
        float prev_gm = (float)prev_gm_sum / CC_NUM_TILES;
        float g_delta = fabsf(cur_gm_f - prev_gm);

        if (g_delta < CC_SPOT_GLOBAL_STAB) {
            int n_spot_dark = 0;
            int n_noisy     = 0;
            for (int i = 0; i < CC_NUM_TILES; i++) {
                float d = (float)s_prev[i] - (float)means[i];  // positive = darkened
                if (d > CC_SPOT_TILE_DELTA) n_spot_dark++;  // strict: mirrors Python's (tile-prev < -N)
                if (d < 0) d = -d;
                if (d >= 10.0f) n_noisy++;
            }
            if (n_spot_dark >= 1 && n_spot_dark <= CC_SPOT_MAX_TILES
                && n_noisy <= CC_SPOT_MAX_NOISY) {
                save_prev(means);
                strcpy(out->label, "process");
                strcpy(out->stage, "SPOT_CHANGE");
                ESP_LOGI(TAG, "SPOT_CHANGE (n_spot=%d g_delta=%.1f) → process",
                         n_spot_dark, g_delta);
                return;
            }
        }
    }

    // QUIET — scene essentially unchanged, just a minor lighting fluctuation
    if (ratio <= CC_QUIET_RATIO) {
        update_model(means);
        save_model();
        save_prev(means);
        strcpy(out->label, "clouds");
        strcpy(out->stage, "QUIET");
        ESP_LOGI(TAG, "QUIET (ratio=%.0f%%) → clouds (suppress)", ratio * 100.0f);
        return;
    }

    // SCENE_DRIFT — tiles dark vs model were already present in previous frame
    // → model is stale (overnight scene change); re-calibrate and upload to be safe
    if (stale_cond) {
        update_model(means);
        s_frames_seen = 0;   // reset warmup — scene changed, re-bootstrap before suppressing
        save_model();        // saves s_frames_seen = 0 via KEY_SEEN
        save_prev(means);
        strcpy(out->label, "process");
        strcpy(out->stage, "SCENE_DRIFT");
        ESP_LOGI(TAG, "SCENE_DRIFT (dark_model=%d new_dark=0) → process + warmup reset",
                 dark_model_tiles);
        return;
    }

    // AMBIGUOUS — default: upload (safety bias, never suppress on doubt)
    save_prev(means);
    strcpy(out->label, "process");
    strcpy(out->stage, "AMBIGUOUS");
    ESP_LOGI(TAG, "AMBIGUOUS (ratio=%.0f%% dark_model=%d new_dark=%d) → process",
             ratio * 100.0f, dark_model_tiles, new_dark_tiles);
}

// ── Public API ────────────────────────────────────────────────────────────────

esp_err_t bw_cc_assess(bw_cc_result_t *out)
{
    memset(out, 0, sizeof(*out));

    esp_err_t err = bw_cam_init(BW_CAM_MODE_LIGHTCHECK);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed: %s", esp_err_to_name(err));
        strcpy(out->label, "process");
        strcpy(out->stage, "CAM_ERR");
        return err;
    }

    camera_fb_t *fb = bw_cam_capture();
    if (!fb || fb->len < (size_t)(CC_FRAME_W * CC_FRAME_H)) {
        ESP_LOGE(TAG, "no valid frame (len=%u)", fb ? (unsigned)fb->len : 0u);
        if (fb) bw_cam_capture_return(fb);
        bw_cam_deinit();
        strcpy(out->label, "process");
        strcpy(out->stage, "CAM_ERR");
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

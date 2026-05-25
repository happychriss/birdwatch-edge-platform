// ─── Cloud-check filter — ESP32-S3 port of the Python pipeline ───────────────
//
// Algorithm matches src/cloud-check/ exactly.  Decision stages (priority order):
//
//   NIGHT       — frame too dark for reliable detection → upload (sun is down)
//   WARMUP      — model not yet bootstrapped; upload always (can't risk missing a bird)
//   DARK_OBJ    — tiles newly dark vs both model AND previous frame → real object → upload
//   QUIET       — ≤25 % dark-anomalous tiles → scene matches model → suppress
//   SCENE_DRIFT — tiles dark vs model but NOT newly dark vs prev → stale model → upload + recalibrate
//   AMBIGUOUS   — default → upload
//
// Background model: 3 photo-buckets (NORMAL / BRIGHT / LOWLIGHT) × 1 scene-bucket.
// Photo-bucket is selected from global Y mean vs BW_BRIGHT/LOWLIGHT_PHOTO_THRESHOLD.
// Only RTC-source frames update the model; PIR frames are evidence-only.
//
// Burst pre-filter runs before the background model.  DUPLICATE requires both
// luma (n_changed==0) and chroma (n_chroma_changed==0) to be unchanged — this
// prevents suppressing PIR events caused by a bird landing on a saturated scene.
//
// Grid: 20×15 = 300 tiles.  All three NVS blobs per photo-bucket:
//   cc_my_n/b/l  : mean_y  (300 float)
//   cc_vy_n/b/l  : var_y   (300 float)
//   cc_mu_n/b/l  : mean_u  (300 float)
//   cc_mv_n/b/l  : mean_v  (300 float)
//   cc_s_n/b/l   : frames_seen (uint16)
//   cc_p / cc_pu / cc_pv : prev-frame Y/U/V tile means (300 uint8 each)
//   cc_pgm       : prev-frame global Y mean (uint8)

#include "cloud_check.h"
#include "config.h"
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

// ── Photo-bucket indexing ────────────────────────────────────────────────────
// NORMAL=0, BRIGHT=1, LOWLIGHT=2  (matches Python photo_bucket_idx())
#define CC_NUM_PB  3
static const char * const PHOTO_BUCKET_NAMES[CC_NUM_PB] = {"NORMAL", "BRIGHT", "LOWLIGHT"};

static inline int photo_bucket_for(uint8_t gm)
{
    if (gm >= BW_BRIGHT_PHOTO_THRESHOLD)   return 1;  // BRIGHT
    if (gm <  BW_LOWLIGHT_PHOTO_THRESHOLD) return 2;  // LOWLIGHT
    return 0;  // NORMAL
}

// ── Background model parameters ──────────────────────────────────────────────
// Values found by parameter sweep over 195-frame real-scene dataset.
#define CC_EMA_ALPHA        0.15f   // background update speed
#define CC_VAR_FLOOR        36.0f   // minimum tile variance (std ≥ 6)
#define CC_INIT_VAR         256.0f  // variance prior (std = 16)
#define CC_INIT_MEAN        128.0f  // mean prior (mid-scale grey)
#define CC_INIT_CHROMA      128.0f  // U/V chroma prior (neutral)
#define CC_Z_THRESHOLD      3.0f    // z-score to flag anomalous
#define CC_QUIET_RATIO      0.25f   // ≤25 % dark-anomalous → QUIET → suppress
#define CC_DARK_DELTA_MODEL 20.0f   // tile ≥20 DN darker than model mean (DARK_OBJ)
#define CC_DARK_DELTA_PREV  20.0f   // tile ≥20 DN darker than previous frame
#define CC_DARK_MIN_TILES        1  // ≥1 qualifying tile triggers DARK_OBJ
#define CC_SCENE_DRIFT_MIN_TILES 4  // SCENE_DRIFT needs ≥4 persistently-dark tiles
#define CC_WARMUP_FRAMES    4       // frames before model is considered bootstrapped
#define CC_NIGHT_THRESHOLD  70      // global mean below this → NIGHT

// ── Burst pre-filter parameters ───────────────────────────────────────────────
// Matches BurstConfig() defaults in burst_filter.py exactly.
#define CC_BURST_BRIGHT_SIM_THR   12
#define CC_BURST_TILE_DIFF_THR    12
#define CC_BURST_DARK_DIFF_THR    12
#define CC_BURST_DUP_MAX_TILES     0
#define CC_BURST_DIFFUSE_MIN_DARK 60
#define CC_BURST_BS_MIN_GM       160
#define CC_BURST_BS_MAX_DARK      35

// ── NVS key names (≤ 14 printable chars + null) ───────────────────────────────
static const char *KEY_MEAN_Y[CC_NUM_PB] = {"cc_my_n", "cc_my_b", "cc_my_l"};
static const char *KEY_VAR_Y [CC_NUM_PB] = {"cc_vy_n", "cc_vy_b", "cc_vy_l"};
static const char *KEY_MEAN_U[CC_NUM_PB] = {"cc_mu_n", "cc_mu_b", "cc_mu_l"};
static const char *KEY_MEAN_V[CC_NUM_PB] = {"cc_mv_n", "cc_mv_b", "cc_mv_l"};
static const char *KEY_SEEN  [CC_NUM_PB] = {"cc_s_n",  "cc_s_b",  "cc_s_l"};
static const char *KEY_PREV_Y  = "cc_p";
static const char *KEY_PREV_U  = "cc_pu";
static const char *KEY_PREV_V  = "cc_pv";
static const char *KEY_PREV_GM = "cc_pgm";

// Legacy K=4 centroid keys (erased on first flash after firmware update)
static const char * const LEGACY_KEYS[] = {
    "cc_m0", "cc_m1", "cc_m2", "cc_m3",
    "cc_v0", "cc_v1", "cc_v2", "cc_v3",
    "cc_s0", "cc_s1", "cc_s2", "cc_s3",
};

// ── In-RAM model state ────────────────────────────────────────────────────────
static float    s_mean_y[CC_NUM_PB][CC_NUM_TILES];
static float    s_var_y [CC_NUM_PB][CC_NUM_TILES];
static float    s_mean_u[CC_NUM_PB][CC_NUM_TILES];
static float    s_mean_v[CC_NUM_PB][CC_NUM_TILES];
static uint16_t s_frames_seen[CC_NUM_PB];

static uint8_t  s_prev_y[CC_NUM_TILES];
static uint8_t  s_prev_u[CC_NUM_TILES];
static uint8_t  s_prev_v[CC_NUM_TILES];
static bool     s_prev_valid    = false;   // true only when Y + U + V all loaded
static uint8_t  s_prev_gm       = 128;
static bool     s_prev_gm_valid = false;

static bool     s_is_rtc_source = false;   // set by bw_cc_set_source()

// ── NVS helpers ───────────────────────────────────────────────────────────────

static void load_model(void)
{
    nvs_handle_t h;
    bool any_loaded = false;

    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        for (int pb = 0; pb < CC_NUM_PB; pb++) {
            size_t sz;

            sz = sizeof(s_mean_y[pb]);
            bool got_my = (nvs_get_blob(h, KEY_MEAN_Y[pb], s_mean_y[pb], &sz) == ESP_OK
                           && sz == sizeof(s_mean_y[pb]));
            sz = sizeof(s_var_y[pb]);
            bool got_vy = (nvs_get_blob(h, KEY_VAR_Y[pb],  s_var_y[pb],  &sz) == ESP_OK
                           && sz == sizeof(s_var_y[pb]));
            sz = sizeof(s_mean_u[pb]);
            bool got_mu = (nvs_get_blob(h, KEY_MEAN_U[pb], s_mean_u[pb], &sz) == ESP_OK
                           && sz == sizeof(s_mean_u[pb]));
            sz = sizeof(s_mean_v[pb]);
            bool got_mv = (nvs_get_blob(h, KEY_MEAN_V[pb], s_mean_v[pb], &sz) == ESP_OK
                           && sz == sizeof(s_mean_v[pb]));

            if (!got_my) for (int i = 0; i < CC_NUM_TILES; i++) s_mean_y[pb][i] = CC_INIT_MEAN;
            if (!got_vy) for (int i = 0; i < CC_NUM_TILES; i++) s_var_y[pb][i]  = CC_INIT_VAR;
            if (!got_mu) for (int i = 0; i < CC_NUM_TILES; i++) s_mean_u[pb][i] = CC_INIT_CHROMA;
            if (!got_mv) for (int i = 0; i < CC_NUM_TILES; i++) s_mean_v[pb][i] = CC_INIT_CHROMA;

            uint16_t seen = 0;
            nvs_get_u16(h, KEY_SEEN[pb], &seen);
            s_frames_seen[pb] = seen;

            if (got_my && got_vy) any_loaded = true;
        }
        nvs_close(h);
    } else {
        for (int pb = 0; pb < CC_NUM_PB; pb++) {
            for (int i = 0; i < CC_NUM_TILES; i++) {
                s_mean_y[pb][i] = CC_INIT_MEAN;
                s_var_y[pb][i]  = CC_INIT_VAR;
                s_mean_u[pb][i] = CC_INIT_CHROMA;
                s_mean_v[pb][i] = CC_INIT_CHROMA;
            }
            s_frames_seen[pb] = 0;
        }
    }

    if (!any_loaded) {
        ESP_LOGI(TAG, "no prior model — initialised fresh (%d photo-buckets)", CC_NUM_PB);
    }
}

static void save_model(int pb)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "model save: nvs_open failed");
        return;
    }
    nvs_set_blob(h, KEY_MEAN_Y[pb], s_mean_y[pb], sizeof(s_mean_y[pb]));
    nvs_set_blob(h, KEY_VAR_Y[pb],  s_var_y[pb],  sizeof(s_var_y[pb]));
    nvs_set_blob(h, KEY_MEAN_U[pb], s_mean_u[pb], sizeof(s_mean_u[pb]));
    nvs_set_blob(h, KEY_MEAN_V[pb], s_mean_v[pb], sizeof(s_mean_v[pb]));
    nvs_set_u16(h,  KEY_SEEN[pb],   s_frames_seen[pb]);
    nvs_commit(h);
    nvs_close(h);
}

static void load_prev(void)
{
    s_prev_valid    = false;
    s_prev_gm_valid = false;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        memset(s_prev_u, 128, CC_NUM_TILES);
        memset(s_prev_v, 128, CC_NUM_TILES);
        return;
    }

    size_t sy  = sizeof(s_prev_y);
    size_t su  = sizeof(s_prev_u);
    size_t sv  = sizeof(s_prev_v);
    bool got_y = (nvs_get_blob(h, KEY_PREV_Y, s_prev_y, &sy) == ESP_OK && sy == sizeof(s_prev_y));
    bool got_u = (nvs_get_blob(h, KEY_PREV_U, s_prev_u, &su) == ESP_OK && su == sizeof(s_prev_u));
    bool got_v = (nvs_get_blob(h, KEY_PREV_V, s_prev_v, &sv) == ESP_OK && sv == sizeof(s_prev_v));
    s_prev_valid = got_y && got_u && got_v;

    if (!got_u) memset(s_prev_u, 128, CC_NUM_TILES);
    if (!got_v) memset(s_prev_v, 128, CC_NUM_TILES);

    uint8_t pgm = 128;
    s_prev_gm_valid = (nvs_get_u8(h, KEY_PREV_GM, &pgm) == ESP_OK);
    s_prev_gm = pgm;
    nvs_close(h);

    if (!s_prev_valid) ESP_LOGI(TAG, "no prior frame — burst+temporal check skipped");
}

static void save_prev(const uint8_t *y, const uint8_t *u, const uint8_t *v, uint8_t gm)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "prev save: nvs_open failed");
        return;
    }
    nvs_set_blob(h, KEY_PREV_Y, y, CC_NUM_TILES);
    if (u) nvs_set_blob(h, KEY_PREV_U, u, CC_NUM_TILES);
    if (v) nvs_set_blob(h, KEY_PREV_V, v, CC_NUM_TILES);
    nvs_set_u8(h,  KEY_PREV_GM, gm);
    nvs_commit(h);
    nvs_close(h);
}

// ── EMA background update (Y with variance + U/V mean-only) ──────────────────
// Matches background.py BackgroundModel.update() exactly.
// Y: new_mean = (1-α)*mean + α*x;  new_var = (1-α)*var + α*(x-new_mean)²
// U/V: simple EMA mean, no variance tracking (only Y z-scores are computed).

static void update_model(int pb, const uint8_t *y, const uint8_t *u, const uint8_t *v)
{
    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x  = (float)y[i];
        float nm = (1.0f - CC_EMA_ALPHA) * s_mean_y[pb][i] + CC_EMA_ALPHA * x;
        float res = x - nm;
        float nv  = (1.0f - CC_EMA_ALPHA) * s_var_y[pb][i] + CC_EMA_ALPHA * res * res;
        s_mean_y[pb][i] = nm;
        s_var_y[pb][i]  = nv < CC_VAR_FLOOR ? CC_VAR_FLOOR : nv;

        if (u) s_mean_u[pb][i] = (1.0f - CC_EMA_ALPHA) * s_mean_u[pb][i]
                                  + CC_EMA_ALPHA * (float)u[i];
        if (v) s_mean_v[pb][i] = (1.0f - CC_EMA_ALPHA) * s_mean_v[pb][i]
                                  + CC_EMA_ALPHA * (float)v[i];
    }
}

// ── Decision pipeline ─────────────────────────────────────────────────────────

static void run_pipeline(const uint8_t *tile_y, const uint8_t *tile_u, const uint8_t *tile_v,
                         bw_cc_result_t *out)
{
    // Global Y mean from JPEG tile means
    uint32_t gm_sum = 0;
    for (int i = 0; i < CC_NUM_TILES; i++) gm_sum += tile_y[i];
    uint8_t global_mean = (uint8_t)(gm_sum / CC_NUM_TILES);
    out->global_mean = global_mean;

    int pb = photo_bucket_for(global_mean);
    strncpy(out->photo_bucket, PHOTO_BUCKET_NAMES[pb], sizeof(out->photo_bucket) - 1);
    out->photo_bucket[sizeof(out->photo_bucket) - 1] = '\0';

    // Emit raw features
    bw_tele_i("global_mean",   (long)global_mean);
    bw_tele_s("photo_bucket",  out->photo_bucket);
    bw_tele_i("scene_bucket",  0);   // K_scene=1: always 0; kept for server back-compat
    bw_tele_arr_u8("tile_means",   tile_y, CC_NUM_TILES);
    if (tile_u) bw_tele_arr_u8("tile_means_u", tile_u, CC_NUM_TILES);
    if (tile_v) bw_tele_arr_u8("tile_means_v", tile_v, CC_NUM_TILES);

    // Model snapshot BEFORE any update (server renders Δm per tile from this)
    {
        uint8_t snap[CC_NUM_TILES];
        for (int i = 0; i < CC_NUM_TILES; i++) {
            float fv = s_mean_y[pb][i] + 0.5f;
            snap[i] = (fv < 0.0f) ? 0u : (fv > 255.0f) ? 255u : (uint8_t)fv;
        }
        bw_tele_arr_u8("model_tile_means", snap, CC_NUM_TILES);

        for (int i = 0; i < CC_NUM_TILES; i++) {
            float fv = s_mean_u[pb][i] + 0.5f;
            snap[i] = (fv < 0.0f) ? 0u : (fv > 255.0f) ? 255u : (uint8_t)fv;
        }
        bw_tele_arr_u8("model_tile_means_u", snap, CC_NUM_TILES);

        for (int i = 0; i < CC_NUM_TILES; i++) {
            float fv = s_mean_v[pb][i] + 0.5f;
            snap[i] = (fv < 0.0f) ? 0u : (fv > 255.0f) ? 255u : (uint8_t)fv;
        }
        bw_tele_arr_u8("model_tile_means_v", snap, CC_NUM_TILES);
    }

    // ── Burst pre-filter ──────────────────────────────────────────────────────
    {
        const char *burst_trigger;
        const char *burst_label;
        int gm_diff = 0, n_changed = 0, n_dark = 0, n_chroma = 0;

        if (!s_prev_valid || !s_prev_gm_valid) {
            burst_trigger = "FIRST";
            burst_label   = "process";
        } else {
            gm_diff = (int)global_mean - (int)s_prev_gm;
            if (gm_diff < 0) gm_diff = -gm_diff;

            if (gm_diff > CC_BURST_BRIGHT_SIM_THR) {
                burst_trigger = "BRIGHTNESS_SHIFT";
                burst_label   = "process";
            } else {
                for (int i = 0; i < CC_NUM_TILES; i++) {
                    int dy = (int)tile_y[i] - (int)s_prev_y[i];
                    if (dy < 0 ? -dy > CC_BURST_TILE_DIFF_THR : dy > CC_BURST_TILE_DIFF_THR)
                        n_changed++;
                    if (-dy > CC_BURST_DARK_DIFF_THR) n_dark++;

                    if (tile_u && tile_v) {
                        int du = (int)tile_u[i] - (int)s_prev_u[i];
                        int dv = (int)tile_v[i] - (int)s_prev_v[i];
                        if (du*du + dv*dv > BW_CC_CHROMA_DELTA_THR_SQ) n_chroma++;
                    }
                }

                // DUPLICATE: both Y and chroma unchanged — truly identical re-fire.
                // Without chroma data, fall back to Y-only (legacy behaviour).
                bool is_dup = (tile_u != NULL)
                    ? (n_changed == 0 && n_chroma == 0)
                    : (n_changed <= CC_BURST_DUP_MAX_TILES);

                if (is_dup) {
                    burst_trigger = "DUPLICATE";
                    burst_label   = "suppress";
                } else if ((uint32_t)global_mean > CC_BURST_BS_MIN_GM
                           && n_dark < CC_BURST_BS_MAX_DARK) {
                    burst_trigger = "BRIGHT_STABLE";
                    burst_label   = "suppress";
                } else if (n_dark >= CC_BURST_DIFFUSE_MIN_DARK) {
                    burst_trigger = "DIFFUSE";
                    burst_label   = "suppress";
                } else {
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
        bw_tele_i("burst_n_chroma",  n_chroma);
        ESP_LOGI(TAG, "BURST %-17s gm_diff=%d n=%d nd=%d nc=%d → %s",
                 burst_trigger, gm_diff, n_changed, n_dark, n_chroma, burst_label);

        if (burst_label[0] == 's') {   // "suppress"
            save_prev(tile_y, tile_u, tile_v, global_mean);
            strcpy(out->label, "clouds");
            strcpy(out->stage, burst_trigger);
            bw_tele_s("result", "clouds");
            bw_tele_s("stage",  burst_trigger);
            return;
        }
    }

    // ── NIGHT ─────────────────────────────────────────────────────────────────
    if (global_mean < CC_NIGHT_THRESHOLD) {
        if (s_is_rtc_source) { update_model(pb, tile_y, tile_u, tile_v); save_model(pb); }
        save_prev(tile_y, tile_u, tile_v, global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "NIGHT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "NIGHT");
        ESP_LOGI(TAG, "NIGHT (gm=%u < %d) → process", global_mean, CC_NIGHT_THRESHOLD);
        return;
    }

    // model.observe(): increment bucket counter for every non-NIGHT frame
    if (s_frames_seen[pb] < 0xFFFF) s_frames_seen[pb]++;
    bool warmup = (s_frames_seen[pb] < CC_WARMUP_FRAMES);
    bw_tele_b("warmup", warmup);

    // ── WARMUP ────────────────────────────────────────────────────────────────
    if (warmup) {
        if (s_is_rtc_source) { update_model(pb, tile_y, tile_u, tile_v); save_model(pb); }
        else { save_model(pb); }   // save frames_seen increment even without model update
        save_prev(tile_y, tile_u, tile_v, global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "WARMUP");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "WARMUP");
        ESP_LOGI(TAG, "WARMUP pb=%d (%u frames seen) rtc=%d → process",
                 pb, s_frames_seen[pb], (int)s_is_rtc_source);
        return;
    }

    // ── Per-tile anomaly analysis ──────────────────────────────────────────────
    // dark_anomalous  : z > threshold AND tile darker than model (QUIET ratio — z-gated)
    // dark_model_tiles: tile ≥ CC_DARK_DELTA_MODEL darker than model (no z-gate)
    // new_dark_tiles  : tile ≥ CC_DARK_DELTA_PREV darker than previous frame (no z-gate)
    //
    // DARK_OBJ chroma gate (when chroma available): tile only counts for dark_model_tiles
    // if chroma also differs from the model — distinguishes a bird (chroma change) from a
    // cloud shadow (Y drops but sky chroma stays the same).  A very large Y drop
    // (>2× threshold) passes the gate unconditionally.
    int dark_anomalous   = 0;
    int dark_model_tiles = 0;
    int new_dark_tiles   = 0;
    int n_chroma_changed = 0;

    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x_y = (float)tile_y[i];
        float m_y = s_mean_y[pb][i];
        float std = sqrtf(s_var_y[pb][i]);

        float z = fabsf(m_y - x_y) / std;
        if (z > CC_Z_THRESHOLD && x_y < m_y) dark_anomalous++;   // z-gated, darker-only

        bool y_dark_from_model = (m_y - x_y > CC_DARK_DELTA_MODEL);
        if (y_dark_from_model) {
            // Chroma gate: bypass when no chroma or when Y drop is very large
            bool chroma_ok = true;
            if (tile_u && tile_v) {
                float du = (float)tile_u[i] - s_mean_u[pb][i];
                float dv = (float)tile_v[i] - s_mean_v[pb][i];
                float dc_sq = du*du + dv*dv;
                chroma_ok = (dc_sq > (float)BW_CC_CHROMA_DOBJ_GATE_SQ)
                         || (m_y - x_y > CC_DARK_DELTA_MODEL * 2.0f);
                if (dc_sq > (float)BW_CC_CHROMA_DOBJ_GATE_SQ) n_chroma_changed++;
            }
            if (chroma_ok) dark_model_tiles++;
        }

        if (s_prev_valid && (float)s_prev_y[i] - x_y > CC_DARK_DELTA_PREV) new_dark_tiles++;
    }

    float ratio = (float)dark_anomalous / CC_NUM_TILES;
    bw_tele_i("dark_anomalous",  (long)dark_anomalous);
    bw_tele_f("ratio",           (double)ratio);
    bw_tele_i("dark_tiles",      (long)dark_model_tiles);
    bw_tele_i("new_dark_tiles",  (long)new_dark_tiles);
    bw_tele_i("n_chroma_changed",(long)n_chroma_changed);
    bw_tele_b("prev_valid",      s_prev_valid);

    bool dark_obj_cond = (dark_model_tiles >= CC_DARK_MIN_TILES) &&
                         (!s_prev_valid || new_dark_tiles >= CC_DARK_MIN_TILES);
    bool stale_cond    = (dark_model_tiles >= CC_SCENE_DRIFT_MIN_TILES) &&
                         s_prev_valid && (new_dark_tiles < CC_DARK_MIN_TILES);

    // ── DARK_OBJ ──────────────────────────────────────────────────────────────
    if (dark_obj_cond) {
        save_prev(tile_y, tile_u, tile_v, global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "DARK_OBJ");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "DARK_OBJ");
        ESP_LOGI(TAG, "DARK_OBJ pb=%d (dark_m=%d new_dark=%d chroma=%d ratio=%.0f%%) → process",
                 pb, dark_model_tiles, new_dark_tiles, n_chroma_changed, ratio * 100.0f);
        return;
    }

    // ── SCENE_DRIFT ───────────────────────────────────────────────────────────
    if (stale_cond) {
        if (s_is_rtc_source) {
            update_model(pb, tile_y, tile_u, tile_v);
            s_frames_seen[pb] = 0;   // reset warmup for this bucket
        }
        save_model(pb);
        save_prev(tile_y, tile_u, tile_v, global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "SCENE_DRIFT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "SCENE_DRIFT");
        ESP_LOGI(TAG, "SCENE_DRIFT pb=%d (dark_m=%d new_dark=0) → process + warmup reset",
                 pb, dark_model_tiles);
        return;
    }

    // ── QUIET ─────────────────────────────────────────────────────────────────
    if (ratio <= CC_QUIET_RATIO) {
        if (s_is_rtc_source) { update_model(pb, tile_y, tile_u, tile_v); }
        save_model(pb);
        save_prev(tile_y, tile_u, tile_v, global_mean);
        strcpy(out->label, "clouds");
        strcpy(out->stage, "QUIET");
        bw_tele_s("result", "clouds");
        bw_tele_s("stage",  "QUIET");
        ESP_LOGI(TAG, "QUIET pb=%d (ratio=%.0f%%) rtc=%d → clouds (suppress)",
                 pb, ratio * 100.0f, (int)s_is_rtc_source);
        return;
    }

    // ── AMBIGUOUS ─────────────────────────────────────────────────────────────
    save_prev(tile_y, tile_u, tile_v, global_mean);
    strcpy(out->label, "process");
    strcpy(out->stage, "AMBIGUOUS");
    bw_tele_s("result", "process");
    bw_tele_s("stage",  "AMBIGUOUS");
    ESP_LOGI(TAG, "AMBIGUOUS pb=%d (ratio=%.0f%% dark_m=%d new_dark=%d) → process",
             pb, ratio * 100.0f, dark_model_tiles, new_dark_tiles);
}

// ── Public API ────────────────────────────────────────────────────────────────

void bw_cc_set_source(bool is_rtc)
{
    s_is_rtc_source = is_rtc;
}

esp_err_t bw_cc_assess(const uint8_t *tile_y, const uint8_t *tile_u,
                       const uint8_t *tile_v, bw_cc_result_t *out)
{
    memset(out, 0, sizeof(*out));
    bw_tele_reset();   // fresh telemetry object for this capture cycle

    load_model();
    load_prev();
    run_pipeline(tile_y, tile_u, tile_v, out);

    uint32_t total_seen = 0;
    for (int pb = 0; pb < CC_NUM_PB; pb++) total_seen += s_frames_seen[pb];
    ESP_LOGI(TAG, "─── result: %-9s  stage: %-15s  pb: %-8s  gm: %u  seen(all): %u ───",
             out->label, out->stage, out->photo_bucket, out->global_mean, (unsigned)total_seen);

    return ESP_OK;
}

void bw_cc_reset(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "cc_reset: nvs_open failed");
        return;
    }
    // New per-photo-bucket keys
    for (int pb = 0; pb < CC_NUM_PB; pb++) {
        nvs_erase_key(h, KEY_MEAN_Y[pb]);
        nvs_erase_key(h, KEY_VAR_Y[pb]);
        nvs_erase_key(h, KEY_MEAN_U[pb]);
        nvs_erase_key(h, KEY_MEAN_V[pb]);
        nvs_erase_key(h, KEY_SEEN[pb]);
    }
    nvs_erase_key(h, KEY_PREV_Y);
    nvs_erase_key(h, KEY_PREV_U);
    nvs_erase_key(h, KEY_PREV_V);
    nvs_erase_key(h, KEY_PREV_GM);
    // Legacy K=4 centroid keys
    for (size_t k = 0; k < sizeof(LEGACY_KEYS)/sizeof(LEGACY_KEYS[0]); k++) {
        nvs_erase_key(h, LEGACY_KEYS[k]);
    }
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "background model cleared (new + legacy NVS keys erased)");
}

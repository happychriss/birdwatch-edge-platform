// ─── Cloud-check filter — ESP32-S3 port of the Python pipeline ───────────────
//
// Algorithm matches src/cloud-check/cloud_check/classifier.py.
// Decision stages (priority order):
//
//   NIGHT       — frame too dark for reliable detection → upload (sun is down)
//   WARMUP      — model not yet bootstrapped; upload always (can't risk missing a bird)
//
// DARK_OBJ (required new_dark_tiles vs prev frame) and SCENE_DRIFT are removed.
// A motionless bird must be detected on a single frame without frame-diff.
// The blob upper-cap (≤5 tiles) rejects diffuse cloud shadows that span many tiles.
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

// ── Scene parameters ─────────────────────────────────────────────────────────
// Below this global mean the scene is too dark to judge, so the frame is
// uploaded rather than suppressed — a dark frame is exactly where a burst rule
// would be least trustworthy.
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
static const char *KEY_PREV_Y  = "cc_p";
static const char *KEY_PREV_U  = "cc_pu";
static const char *KEY_PREV_V  = "cc_pv";
static const char *KEY_PREV_GM = "cc_pgm";

// Keys of retired models, erased by bw_cc_reset() on a firmware update.  The
// cc_my_/cc_vy_/cc_mu_/cc_mv_/cc_s_ set belonged to the per-bucket background
// model (~3.6 KB of a 24 KB NVS partition); it is gone, so reclaim the space
// even on a device that was flashed without a full erase.
static const char * const LEGACY_KEYS[] = {
    "cc_m0", "cc_m1", "cc_m2", "cc_m3",
    "cc_v0", "cc_v1", "cc_v2", "cc_v3",
    "cc_s0", "cc_s1", "cc_s2", "cc_s3",
    "cc_my_n", "cc_my_b", "cc_my_l",
    "cc_vy_n", "cc_vy_b", "cc_vy_l",
    "cc_mu_n", "cc_mu_b", "cc_mu_l",
    "cc_mv_n", "cc_mv_b", "cc_mv_l",
    "cc_s_n",  "cc_s_b",  "cc_s_l",
};

// ── In-RAM model state ────────────────────────────────────────────────────────
static uint8_t  s_prev_y[CC_NUM_TILES];
static uint8_t  s_prev_u[CC_NUM_TILES];
static uint8_t  s_prev_v[CC_NUM_TILES];
static bool     s_prev_valid    = false;   // true only when Y + U + V all loaded
static uint8_t  s_prev_gm       = 128;
static bool     s_prev_gm_valid = false;

static bool     s_is_rtc_source = false;   // set by bw_cc_set_source()

// ── NVS helpers ───────────────────────────────────────────────────────────────

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

// ── Decision pipeline ─────────────────────────────────────────────────────────

static void run_pipeline(const uint8_t *tile_y, const uint8_t *tile_u, const uint8_t *tile_v,
                         bw_cc_result_t *out)
{
    // Global Y mean from JPEG tile means
    uint32_t gm_sum = 0;
    for (int i = 0; i < CC_NUM_TILES; i++) gm_sum += tile_y[i];
    uint8_t global_mean = (uint8_t)(gm_sum / CC_NUM_TILES);
    out->global_mean = global_mean;

    // Emit raw features
    bw_tele_i("global_mean",   (long)global_mean);
    bw_tele_arr_u8("tile_means",   tile_y, CC_NUM_TILES);
    if (tile_u) bw_tele_arr_u8("tile_means_u", tile_u, CC_NUM_TILES);
    if (tile_v) bw_tele_arr_u8("tile_means_v", tile_v, CC_NUM_TILES);

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
        save_prev(tile_y, tile_u, tile_v, global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "NIGHT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "NIGHT");
        ESP_LOGI(TAG, "NIGHT (gm=%u < %d) → process", global_mean, CC_NIGHT_THRESHOLD);
        return;
    }

    // ── Everything past the burst filter is now the server's problem ─────────
    // The per-tile background model that used to run here (z-score vs a
    // per-bucket EMA, DARK_BLOB / QUIET / AMBIGUOUS) is retired.  Measured on
    // 87 labelled birds it reached 32% recall at a 10% false-positive rate,
    // against a requirement of 100% — it could not separate birds on this
    // scene, so keeping it would spend NVS, CPU and warm-up time producing a
    // verdict nothing consumes.  Suppression now happens BEFORE the camera,
    // from the clock alone (see presuppress.c).
    save_prev(tile_y, tile_u, tile_v, global_mean);
    strcpy(out->label, "process");
    strcpy(out->stage, "SAFE");
    bw_tele_s("result", "process");
    bw_tele_s("stage",  "SAFE");
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

    load_prev();
    run_pipeline(tile_y, tile_u, tile_v, out);

    ESP_LOGI(TAG, "─── result: %-9s  stage: %-15s  gm: %u ───",
             out->label, out->stage, out->global_mean);

    return ESP_OK;
}

void bw_cc_reset(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "cc_reset: nvs_open failed");
        return;
    }
    nvs_erase_key(h, KEY_PREV_Y);
    nvs_erase_key(h, KEY_PREV_U);
    nvs_erase_key(h, KEY_PREV_V);
    nvs_erase_key(h, KEY_PREV_GM);
    // Retired-model keys (see LEGACY_KEYS)
    for (size_t k = 0; k < sizeof(LEGACY_KEYS)/sizeof(LEGACY_KEYS[0]); k++) {
        nvs_erase_key(h, LEGACY_KEYS[k]);
    }
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "cc NVS state cleared (prev-frame + retired-model keys erased)");
}

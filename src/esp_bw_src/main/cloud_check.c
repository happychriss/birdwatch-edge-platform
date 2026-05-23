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

// ── Lighting-scenario bucket count ───────────────────────────────────────────
// 4 buckets match Python K=4 k-means model.
// Each frame is assigned to its nearest centroid before the pipeline runs.
#define CC_NUM_BUCKETS 4

// ── NVS key names ─────────────────────────────────────────────────────────────
// "cc_m0..3": per-bucket tile means     (300 × float = 1200 B each)
// "cc_v0..3": per-bucket tile variances (300 × float = 1200 B each)
// "cc_s0..3": per-bucket frames-seen    (uint16 each)
// "cc_p"    : previous-frame tile means (300 × uint8 = 300 B)
// "cc_pgm"  : previous-frame global mean (uint8)
// Total NVS usage: 4 × 2400 + 300 + 1 ≈ 9.9 KB (fits default 24 KB partition)
static const char *KEY_PREV    = "cc_p";
static const char *KEY_PREV_GM = "cc_pgm";
static const char *KEY_MEAN[CC_NUM_BUCKETS] = {"cc_m0", "cc_m1", "cc_m2", "cc_m3"};
static const char *KEY_VAR [CC_NUM_BUCKETS] = {"cc_v0", "cc_v1", "cc_v2", "cc_v3"};
static const char *KEY_SEEN[CC_NUM_BUCKETS] = {"cc_s0", "cc_s1", "cc_s2", "cc_s3"};

// ── In-RAM model state ────────────────────────────────────────────────────────
static float    s_mean[CC_NUM_BUCKETS][CC_NUM_TILES];
static float    s_var[CC_NUM_BUCKETS][CC_NUM_TILES];
static uint16_t s_frames_seen[CC_NUM_BUCKETS];  // per-bucket non-NIGHT frame count
static uint8_t  s_prev[CC_NUM_TILES];
static bool     s_prev_valid    = false;
static uint8_t  s_prev_gm       = 128;
static bool     s_prev_gm_valid = false;

// ── K=4 lighting-scenario centroids ──────────────────────────────────────────
// Computed offline from 324 background frames in the production database.
// nearest_bucket() selects the model to compare each frame against.
// Regenerate via: python compute_centroids.py  (src/cloud-check/)
static const float CC_CENTROIDS[CC_NUM_BUCKETS][CC_NUM_TILES] = {
    /* bucket 0: n=48 gm=56.6 — dark / dim */
    {55.17f, 54.37f, 53.33f, 51.77f, 50.46f, 49.56f, 49.02f, 48.00f, 46.90f, 46.25f,
     47.02f, 47.77f, 48.81f, 49.69f, 50.12f, 50.67f, 51.90f, 53.60f, 55.67f, 59.98f,
     56.15f, 54.46f, 53.19f, 51.94f, 50.96f, 50.00f, 49.77f, 49.23f, 48.31f, 47.65f,
     48.21f, 48.85f, 49.73f, 50.04f, 50.17f, 50.75f, 51.50f, 53.04f, 65.23f, 104.54f,
     67.46f, 59.25f, 53.33f, 52.40f, 51.62f, 50.71f, 50.35f, 50.23f, 49.69f, 49.29f,
     50.02f, 50.67f, 51.23f, 51.08f, 52.19f, 53.92f, 50.60f, 51.46f, 54.75f, 98.48f,
     60.83f, 67.15f, 62.08f, 58.79f, 59.31f, 46.17f, 48.77f, 51.21f, 52.54f, 55.23f,
     58.04f, 60.10f, 58.40f, 61.37f, 64.12f, 59.27f, 53.00f, 49.81f, 52.29f, 93.65f,
     63.58f, 68.04f, 61.62f, 60.81f, 68.10f, 56.60f, 47.06f, 42.63f, 49.62f, 55.52f,
     64.23f, 70.02f, 69.23f, 67.96f, 61.21f, 60.08f, 53.15f, 49.08f, 47.98f, 78.31f,
     118.52f, 83.65f, 65.04f, 69.33f, 78.85f, 66.19f, 49.00f, 62.48f, 55.71f, 57.48f,
     59.77f, 66.04f, 63.73f, 67.06f, 57.02f, 63.04f, 53.44f, 50.58f, 50.50f, 63.58f,
     108.56f, 80.50f, 69.10f, 75.88f, 83.10f, 71.08f, 50.38f, 66.87f, 62.54f, 61.35f,
     64.00f, 73.92f, 75.92f, 71.58f, 67.44f, 57.85f, 49.31f, 51.42f, 55.62f, 50.52f,
     88.83f, 75.40f, 68.19f, 75.58f, 74.48f, 71.42f, 58.44f, 72.29f, 65.77f, 55.65f,
     53.06f, 63.10f, 84.52f, 68.46f, 53.71f, 44.02f, 39.48f, 44.38f, 55.15f, 53.12f,
     60.65f, 71.75f, 65.42f, 70.90f, 78.10f, 65.23f, 45.94f, 55.08f, 55.77f, 45.10f,
     44.65f, 51.35f, 70.46f, 53.83f, 44.90f, 42.92f, 42.44f, 41.46f, 45.56f, 57.88f,
     65.85f, 72.90f, 64.54f, 65.56f, 73.85f, 61.90f, 45.25f, 44.25f, 45.46f, 44.92f,
     44.00f, 49.98f, 52.83f, 60.06f, 41.04f, 41.10f, 46.25f, 45.08f, 44.10f, 58.52f,
     72.42f, 61.85f, 64.29f, 66.71f, 62.83f, 51.40f, 46.67f, 40.52f, 40.71f, 45.83f,
     46.98f, 56.21f, 48.50f, 44.31f, 41.50f, 39.75f, 44.46f, 47.79f, 49.75f, 56.79f,
     62.04f, 49.71f, 45.81f, 44.15f, 46.85f, 49.31f, 44.52f, 38.31f, 43.54f, 44.69f,
     45.13f, 41.75f, 52.46f, 49.33f, 48.40f, 41.02f, 44.67f, 56.10f, 61.96f, 81.12f,
     53.29f, 49.38f, 44.88f, 43.98f, 41.75f, 48.06f, 50.98f, 49.77f, 53.65f, 46.23f,
     42.40f, 27.79f, 58.75f, 64.69f, 59.25f, 50.96f, 47.54f, 57.94f, 70.35f, 111.29f,
     53.31f, 49.90f, 47.27f, 45.63f, 48.71f, 54.94f, 53.54f, 55.83f, 57.73f, 54.42f,
     50.73f, 42.06f, 48.71f, 62.23f, 60.52f, 64.21f, 66.29f, 75.75f, 90.19f, 105.31f,
     52.48f, 51.27f, 50.98f, 47.67f, 55.46f, 55.04f, 54.81f, 55.17f, 51.98f, 47.46f,
     45.79f, 42.29f, 46.58f, 50.21f, 47.83f, 47.63f, 50.10f, 53.17f, 59.44f, 68.29f},
    /* bucket 1: n=106 gm=101.8 — mid-light */
    {103.05f, 101.73f, 100.08f, 97.95f, 95.86f, 94.70f, 93.73f, 92.15f, 90.36f, 89.18f,
     89.04f, 90.17f, 91.61f, 92.49f, 92.64f, 92.84f, 94.11f, 95.72f, 96.85f, 97.76f,
     106.08f, 103.49f, 101.57f, 100.32f, 98.89f, 97.95f, 97.69f, 97.10f, 95.65f, 94.57f,
     94.44f, 95.17f, 96.10f, 95.98f, 95.25f, 95.57f, 96.11f, 96.09f, 94.33f, 91.92f,
     105.40f, 104.32f, 102.69f, 102.11f, 101.90f, 101.87f, 102.01f, 102.60f, 102.28f, 100.83f,
     100.50f, 100.88f, 100.93f, 99.63f, 98.47f, 96.23f, 90.71f, 86.08f, 80.41f, 79.59f,
     108.92f, 108.02f, 108.19f, 109.92f, 123.21f, 142.16f, 141.09f, 139.90f, 137.88f, 130.23f,
     124.88f, 119.50f, 106.40f, 101.54f, 95.99f, 91.91f, 89.10f, 86.64f, 83.90f, 76.28f,
     105.88f, 113.38f, 118.62f, 123.50f, 166.50f, 182.07f, 185.15f, 186.61f, 187.25f, 183.77f,
     180.01f, 168.75f, 113.44f, 98.75f, 98.91f, 99.10f, 100.00f, 93.42f, 85.89f, 83.56f,
     108.52f, 115.49f, 122.04f, 146.25f, 226.69f, 237.06f, 239.77f, 239.83f, 238.08f, 225.95f,
     225.25f, 211.05f, 147.08f, 109.16f, 101.01f, 92.84f, 99.75f, 95.64f, 87.79f, 84.26f,
     103.56f, 116.25f, 117.66f, 134.45f, 205.42f, 228.24f, 231.12f, 241.19f, 240.02f, 182.59f,
     228.54f, 228.74f, 173.23f, 115.55f, 101.22f, 97.25f, 92.75f, 93.39f, 88.32f, 82.68f,
     52.05f, 83.77f, 63.85f, 72.55f, 82.99f, 114.61f, 135.58f, 214.26f, 173.12f, 110.16f,
     167.01f, 197.88f, 170.19f, 112.72f, 98.96f, 94.25f, 85.40f, 87.13f, 83.19f, 79.55f,
     26.28f, 25.73f, 28.48f, 35.18f, 34.16f, 55.06f, 95.95f, 173.67f, 136.31f, 71.20f,
     88.00f, 137.57f, 141.67f, 101.61f, 96.28f, 90.62f, 83.76f, 82.06f, 80.97f, 78.69f,
     24.88f, 20.32f, 19.37f, 19.26f, 25.48f, 40.11f, 78.59f, 116.61f, 101.75f, 58.36f,
     42.05f, 76.03f, 81.13f, 81.64f, 81.30f, 87.33f, 83.32f, 77.09f, 79.19f, 79.16f,
     16.73f, 16.07f, 16.91f, 17.97f, 31.21f, 43.46f, 73.65f, 90.78f, 92.53f, 82.78f,
     46.58f, 49.75f, 37.75f, 47.11f, 64.26f, 74.02f, 73.55f, 77.12f, 82.25f, 82.47f,
     18.27f, 15.42f, 16.64f, 20.86f, 47.61f, 68.79f, 84.24f, 92.34f, 101.04f, 98.94f,
     62.82f, 60.21f, 67.51f, 58.15f, 65.62f, 68.19f, 72.41f, 75.60f, 75.64f, 76.75f,
     47.50f, 47.74f, 49.34f, 55.83f, 80.70f, 99.67f, 117.18f, 125.92f, 136.98f, 116.82f,
     90.63f, 94.94f, 103.93f, 85.39f, 80.84f, 81.44f, 81.72f, 81.43f, 84.21f, 87.09f,
     92.31f, 92.35f, 87.14f, 92.73f, 99.11f, 110.06f, 122.91f, 136.09f, 139.86f, 125.25f,
     105.95f, 104.77f, 116.31f, 107.15f, 98.83f, 93.88f, 93.33f, 91.52f, 90.19f, 90.42f,
     102.81f, 103.86f, 103.70f, 106.42f, 107.15f, 112.71f, 126.54f, 133.60f, 125.34f, 116.30f,
     109.45f, 107.34f, 112.70f, 113.64f, 104.84f, 100.13f, 99.15f, 96.47f, 94.86f, 97.36f},
    /* bucket 2: n=35 gm=117.9 — mid-bright */
    {112.80f, 111.69f, 110.14f, 108.51f, 106.91f, 106.40f, 105.83f, 104.94f, 103.91f, 103.57f,
     104.00f, 105.89f, 107.89f, 109.66f, 110.34f, 111.46f, 113.89f, 117.34f, 122.66f, 131.89f,
     115.00f, 112.51f, 110.80f, 109.91f, 109.03f, 108.26f, 108.80f, 108.29f, 107.86f, 107.60f,
     108.49f, 109.89f, 111.91f, 112.54f, 112.57f, 113.54f, 115.26f, 117.69f, 136.80f, 198.40f,
     135.89f, 119.97f, 110.74f, 111.57f, 111.20f, 111.03f, 111.51f, 112.11f, 112.40f, 112.57f,
     113.49f, 114.91f, 116.06f, 115.66f, 117.77f, 120.63f, 110.86f, 107.97f, 110.23f, 190.83f,
     124.69f, 129.97f, 119.00f, 124.43f, 126.74f, 91.57f, 99.31f, 114.14f, 119.51f, 125.09f,
     129.03f, 132.71f, 129.74f, 134.86f, 138.37f, 129.49f, 115.71f, 113.54f, 114.46f, 177.17f,
     123.00f, 130.26f, 120.49f, 131.57f, 144.89f, 126.89f, 100.66f, 92.09f, 113.54f, 125.80f,
     137.80f, 147.11f, 143.86f, 140.83f, 132.26f, 139.31f, 130.23f, 115.57f, 102.31f, 150.80f,
     182.11f, 146.06f, 124.37f, 142.03f, 155.54f, 134.66f, 98.51f, 139.43f, 128.43f, 134.06f,
     134.69f, 139.69f, 131.11f, 145.51f, 122.20f, 142.29f, 127.37f, 115.31f, 106.77f, 126.40f,
     176.46f, 144.31f, 126.43f, 145.57f, 158.66f, 148.14f, 101.03f, 152.14f, 146.89f, 148.43f,
     150.00f, 151.77f, 152.26f, 155.97f, 143.34f, 139.97f, 111.71f, 111.09f, 107.00f, 88.43f,
     151.34f, 136.49f, 124.37f, 142.14f, 149.09f, 152.69f, 131.43f, 157.11f, 147.69f, 140.97f,
     139.83f, 136.97f, 153.83f, 153.17f, 127.51f, 102.20f, 90.74f, 98.89f, 121.66f, 115.29f,
     109.06f, 127.83f, 120.74f, 135.89f, 148.89f, 137.03f, 103.09f, 122.46f, 123.86f, 108.94f,
     104.97f, 114.03f, 138.40f, 111.89f, 101.17f, 85.49f, 84.09f, 92.91f, 104.57f, 136.54f,
     120.11f, 127.14f, 110.71f, 119.37f, 136.11f, 130.83f, 94.23f, 90.83f, 95.51f, 100.91f,
     124.40f, 132.63f, 126.11f, 137.40f, 89.57f, 74.51f, 77.71f, 77.63f, 99.11f, 138.57f,
     125.23f, 113.14f, 104.26f, 111.74f, 110.09f, 94.89f, 89.94f, 72.06f, 78.74f, 90.23f,
     97.63f, 116.29f, 95.94f, 85.86f, 90.37f, 74.80f, 78.43f, 78.94f, 99.20f, 129.49f,
     105.29f, 89.23f, 83.11f, 86.74f, 94.23f, 93.94f, 87.49f, 80.57f, 73.86f, 86.11f,
     108.34f, 111.14f, 136.37f, 121.20f, 106.11f, 96.60f, 100.23f, 108.17f, 128.23f, 166.09f,
     87.00f, 90.31f, 85.43f, 83.37f, 75.51f, 85.06f, 96.80f, 99.31f, 95.00f, 85.31f,
     104.74f, 88.86f, 146.00f, 155.06f, 135.46f, 130.23f, 127.00f, 131.86f, 151.37f, 219.80f,
     89.03f, 88.66f, 83.83f, 79.11f, 75.11f, 91.00f, 95.74f, 95.09f, 95.77f, 100.23f,
     104.31f, 105.97f, 123.37f, 144.43f, 152.63f, 160.51f, 163.17f, 171.20f, 191.06f, 205.71f,
     85.80f, 87.26f, 86.69f, 71.03f, 86.74f, 90.66f, 92.26f, 89.11f, 87.86f, 90.77f,
     95.77f, 100.94f, 110.00f, 118.63f, 124.29f, 127.40f, 129.80f, 133.11f, 133.83f, 153.11f},
    /* bucket 3: n=135 gm=154.5 — bright/sun */
    {189.63f, 187.76f, 185.77f, 183.27f, 181.27f, 181.24f, 180.90f, 179.32f, 176.96f, 174.47f,
     173.13f, 173.99f, 175.56f, 176.45f, 176.51f, 176.76f, 178.27f, 180.04f, 181.49f, 182.63f,
     193.02f, 190.36f, 187.88f, 186.59f, 185.48f, 185.76f, 185.96f, 186.05f, 183.99f, 181.28f,
     179.81f, 180.14f, 181.15f, 180.99f, 180.21f, 180.45f, 181.01f, 177.01f, 172.36f, 169.59f,
     177.44f, 177.58f, 181.87f, 187.73f, 187.44f, 189.09f, 190.90f, 194.56f, 194.53f, 189.28f,
     187.13f, 187.28f, 187.16f, 185.67f, 182.08f, 174.07f, 167.12f, 165.30f, 162.99f, 152.16f,
     171.06f, 172.50f, 170.77f, 170.93f, 177.68f, 196.41f, 198.82f, 207.08f, 209.40f, 199.75f,
     195.56f, 192.27f, 187.13f, 181.76f, 171.03f, 170.99f, 168.86f, 163.47f, 157.96f, 150.30f,
     119.19f, 135.75f, 157.12f, 172.90f, 207.85f, 219.47f, 218.52f, 217.63f, 217.47f, 216.96f,
     216.07f, 211.62f, 186.81f, 183.26f, 177.92f, 167.67f, 168.36f, 163.66f, 162.79f, 164.04f,
     114.50f, 114.27f, 115.12f, 152.98f, 249.75f, 251.72f, 252.38f, 252.43f, 250.04f, 235.13f,
     238.60f, 232.70f, 193.50f, 173.59f, 164.68f, 146.16f, 166.36f, 165.61f, 163.24f, 166.71f,
     102.34f, 109.30f, 106.04f, 142.56f, 207.44f, 224.53f, 229.81f, 247.43f, 240.59f, 182.12f,
     231.46f, 240.56f, 204.16f, 170.30f, 165.49f, 161.74f, 155.41f, 166.04f, 163.65f, 165.07f,
     63.46f, 87.28f, 64.01f, 83.60f, 96.63f, 111.41f, 138.01f, 221.00f, 177.26f, 99.11f,
     173.21f, 201.84f, 196.30f, 160.07f, 164.34f, 163.01f, 151.41f, 163.33f, 162.41f, 163.16f,
     44.19f, 41.43f, 41.24f, 52.16f, 53.32f, 65.67f, 90.84f, 173.37f, 127.09f, 64.89f,
     89.36f, 135.57f, 146.37f, 138.84f, 157.24f, 164.17f, 152.72f, 160.21f, 164.84f, 166.57f,
     40.07f, 35.79f, 39.36f, 36.63f, 41.36f, 52.47f, 70.37f, 104.11f, 92.42f, 54.59f,
     63.67f, 91.52f, 83.10f, 101.38f, 121.36f, 157.73f, 161.10f, 153.91f, 168.64f, 172.00f,
     27.86f, 29.38f, 31.76f, 34.47f, 43.31f, 47.22f, 62.02f, 68.33f, 71.43f, 81.28f,
     75.27f, 74.28f, 42.43f, 34.17f, 88.61f, 130.24f, 147.95f, 163.32f, 185.06f, 188.87f,
     31.24f, 30.84f, 31.68f, 33.97f, 52.11f, 75.81f, 92.24f, 101.79f, 113.15f, 112.17f,
     74.65f, 79.00f, 89.41f, 76.56f, 113.54f, 134.57f, 153.94f, 170.27f, 178.77f, 182.56f,
     79.93f, 75.04f, 76.07f, 87.33f, 117.11f, 138.25f, 156.92f, 169.25f, 177.96f, 158.30f,
     134.77f, 138.05f, 139.88f, 125.38f, 146.24f, 162.26f, 176.54f, 180.78f, 192.36f, 199.88f,
     168.21f, 163.45f, 153.06f, 166.52f, 178.36f, 185.36f, 188.27f, 195.21f, 188.21f, 178.73f,
     173.05f, 173.10f, 179.00f, 178.36f, 185.75f, 186.80f, 192.75f, 194.96f, 195.40f, 198.96f,
     192.76f, 189.87f, 188.09f, 194.74f, 192.27f, 190.30f, 197.90f, 202.24f, 195.81f, 190.27f,
     186.52f, 182.64f, 185.21f, 188.81f, 189.10f, 193.38f, 195.84f, 196.13f, 196.71f, 204.24f},
};

// ── Nearest-centroid bucket selection ─────────────────────────────────────────
// Computes squared L2 distance from tile_means to each centroid.
// Returns the bucket index [0, CC_NUM_BUCKETS) with smallest distance.

static int nearest_bucket(const uint8_t *means)
{
    int best = 0;
    float best_dist = 1e30f;
    for (int b = 0; b < CC_NUM_BUCKETS; b++) {
        float dist = 0.0f;
        for (int i = 0; i < CC_NUM_TILES; i++) {
            float d = (float)means[i] - CC_CENTROIDS[b][i];
            dist += d * d;
        }
        if (dist < best_dist) {
            best_dist = dist;
            best = b;
        }
    }
    return best;
}

// ── NVS helpers ───────────────────────────────────────────────────────────────

static void load_model(void)
{
    nvs_handle_t h;
    bool any_loaded = false;

    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        for (int b = 0; b < CC_NUM_BUCKETS; b++) {
            size_t sm = sizeof(s_mean[b]);
            bool got_m = (nvs_get_blob(h, KEY_MEAN[b], s_mean[b], &sm) == ESP_OK
                          && sm == sizeof(s_mean[b]));

            size_t sv = sizeof(s_var[b]);
            bool got_v = (nvs_get_blob(h, KEY_VAR[b], s_var[b], &sv) == ESP_OK
                          && sv == sizeof(s_var[b]));

            if (!got_m) {
                for (int i = 0; i < CC_NUM_TILES; i++) s_mean[b][i] = CC_INIT_MEAN;
            }
            if (!got_v) {
                for (int i = 0; i < CC_NUM_TILES; i++) s_var[b][i]  = CC_INIT_VAR;
            }

            uint16_t seen = 0;
            nvs_get_u16(h, KEY_SEEN[b], &seen);
            s_frames_seen[b] = seen;

            if (got_m && got_v) any_loaded = true;
        }
        nvs_close(h);
    } else {
        for (int b = 0; b < CC_NUM_BUCKETS; b++) {
            for (int i = 0; i < CC_NUM_TILES; i++) {
                s_mean[b][i] = CC_INIT_MEAN;
                s_var[b][i]  = CC_INIT_VAR;
            }
            s_frames_seen[b] = 0;
        }
    }

    if (!any_loaded) {
        ESP_LOGI(TAG, "no prior model — initialised fresh (%d buckets)", CC_NUM_BUCKETS);
    }
}

static void save_model(int b)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "model save: nvs_open failed");
        return;
    }
    nvs_set_blob(h, KEY_MEAN[b], s_mean[b], sizeof(s_mean[b]));
    nvs_set_blob(h, KEY_VAR[b],  s_var[b],  sizeof(s_var[b]));
    nvs_set_u16(h,  KEY_SEEN[b], s_frames_seen[b]);
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

static void update_model(int b, const uint8_t *means)
{
    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x        = (float)means[i];
        float new_mean = (1.0f - CC_EMA_ALPHA) * s_mean[b][i] + CC_EMA_ALPHA * x;
        float residual = x - new_mean;
        float new_var  = (1.0f - CC_EMA_ALPHA) * s_var[b][i] + CC_EMA_ALPHA * residual * residual;
        s_mean[b][i] = new_mean;
        s_var[b][i]  = new_var < CC_VAR_FLOOR ? CC_VAR_FLOOR : new_var;
    }
}

// ── Decision pipeline ─────────────────────────────────────────────────────────
// Matches classifier.py classify() + pipeline.py run_stream() exactly.
//
// Key facts:
// 1. z-score is ABSOLUTE for QUIET ratio but NOT applied to dark_model_tiles /
//    new_dark_tiles (DARK_OBJ). Removing the z-gate lets us catch birds that are
//    35-90 DN darker than the model (below z=3 even with tighter scene buckets).
// 2. dark_model_tiles and new_dark_tiles are counted independently — they can
//    be DIFFERENT tiles. DARK_OBJ fires when ≥1 of each exists (not the same tile).
// 3. s_frames_seen[b] is incremented BEFORE the warmup check (mirrors Python's
//    model.observe() call before classify()). b is the nearest-centroid bucket.
// 4. nearest_bucket() assigns each frame to one of K=4 pre-computed lighting-
//    scenario centroids, giving per-bucket std≈28 DN vs 52 DN for a single bucket.

static void run_pipeline(const uint8_t *means, bw_cc_result_t *out)
{
    uint32_t gm_sum = 0;
    for (int i = 0; i < CC_NUM_TILES; i++) gm_sum += means[i];
    uint32_t global_mean = gm_sum / CC_NUM_TILES;
    out->global_mean = (uint8_t)global_mean;
    bw_tele_i("global_mean", (long)global_mean);
    bw_tele_arr_u8("tile_means", means, CC_NUM_TILES);

    // Nearest-centroid bucket for this frame's lighting scenario.
    int b = nearest_bucket(means);
    bw_tele_i("scene_bucket", (long)b);

    // Background model means (pre-update snapshot) — lets the server render Δm per tile.
    // s_mean[b][] was loaded from NVS before run_pipeline(); this is the state that
    // z-scores are computed from.  Rounded to uint8 for compact JSON.
    {
        uint8_t model_m[CC_NUM_TILES];
        for (int i = 0; i < CC_NUM_TILES; i++) {
            float v = s_mean[b][i] + 0.5f;
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
        update_model(b, means);
        save_model(b);
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "NIGHT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "NIGHT");
        ESP_LOGI(TAG, "NIGHT (global_mean=%" PRIu32 " < %d) → process", global_mean, CC_NIGHT_THRESHOLD);
        return;
    }

    // Mirror Python's model.observe(): increment bucket counter for every non-NIGHT
    // frame, BEFORE the warmup check.  NIGHT frames are excluded so they don't
    // inflate the bucket count before the model can reliably suppress.
    if (s_frames_seen[b] < 0xFFFF) s_frames_seen[b]++;

    // WARMUP — model not yet bootstrapped
    bool warmup = (s_frames_seen[b] < CC_WARMUP_FRAMES);
    bw_tele_b("warmup", warmup);
    if (warmup) {
        update_model(b, means);
        save_model(b);
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "WARMUP");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "WARMUP");
        ESP_LOGI(TAG, "WARMUP bucket=%d (%u frames seen) → process", b, s_frames_seen[b]);
        return;
    }

    // Per-tile anomaly analysis.
    // dark_anomalous  : z > threshold AND tile darker than model (QUIET ratio — z-gated)
    // dark_model_tiles: tile ≥ CC_DARK_DELTA_MODEL darker than model (no z-gate — catches birds)
    // new_dark_tiles  : tile ≥ CC_DARK_DELTA_PREV darker than previous frame (no z-gate)
    //
    // The z-gate is intentionally removed from dark_model_tiles and new_dark_tiles.
    // With scene-lighting buckets (std≈28 DN) z=3 needs |Δm|>84 DN but birds are
    // only 35-90 DN darker.  The absolute delta threshold + temporal check provides
    // sufficient protection against noise without the z-gate.
    // QUIET ratio retains z-gate: bright illumination shifts must not prevent suppression.
    int dark_anomalous   = 0;
    int dark_model_tiles = 0;
    int new_dark_tiles   = 0;

    for (int i = 0; i < CC_NUM_TILES; i++) {
        float x   = (float)means[i];
        float m   = s_mean[b][i];
        float std = sqrtf(s_var[b][i]);

        float z     = fabsf(m - x) / std;
        bool z_anom = (z > CC_Z_THRESHOLD);

        if (z_anom && x < m) dark_anomalous++;  // z-gated: only darker-than-model (QUIET ratio)
        if (m - x > CC_DARK_DELTA_MODEL) dark_model_tiles++;   // no z-gate (DARK_OBJ)
        if (s_prev_valid && (float)s_prev[i] - x > CC_DARK_DELTA_PREV) new_dark_tiles++;  // no z-gate
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
        update_model(b, means);
        save_model(b);
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "clouds");
        strcpy(out->stage, "QUIET");
        bw_tele_s("result", "clouds");
        bw_tele_s("stage",  "QUIET");
        ESP_LOGI(TAG, "QUIET bucket=%d (ratio=%.0f%%) → clouds (suppress)", b, ratio * 100.0f);
        return;
    }

    // SCENE_DRIFT — tiles dark vs model were already present in previous frame
    // → model is stale (overnight scene change); re-calibrate and upload to be safe
    if (stale_cond) {
        update_model(b, means);
        s_frames_seen[b] = 0;   // reset warmup for this bucket — scene changed
        save_model(b);
        save_prev(means, (uint8_t)global_mean);
        strcpy(out->label, "process");
        strcpy(out->stage, "SCENE_DRIFT");
        bw_tele_s("result", "process");
        bw_tele_s("stage",  "SCENE_DRIFT");
        ESP_LOGI(TAG, "SCENE_DRIFT bucket=%d (dark_model=%d new_dark=0) → process + warmup reset",
                 b, dark_model_tiles);
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
    for (int b = 0; b < CC_NUM_BUCKETS; b++) {
        nvs_erase_key(h, KEY_MEAN[b]);
        nvs_erase_key(h, KEY_VAR[b]);
        nvs_erase_key(h, KEY_SEEN[b]);
    }
    nvs_erase_key(h, KEY_PREV);
    nvs_erase_key(h, KEY_PREV_GM);
    // Also erase legacy single-bucket keys in case of upgrade from old firmware
    nvs_erase_key(h, "cc_m");
    nvs_erase_key(h, "cc_v");
    nvs_erase_key(h, "cc_seen");
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "background model cleared (%d buckets)", CC_NUM_BUCKETS);
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

    uint32_t total_seen = 0;
    for (int b = 0; b < CC_NUM_BUCKETS; b++) total_seen += s_frames_seen[b];
    ESP_LOGI(TAG, "─── result: %-9s  stage: %-11s  seen(all): %u  prev: %s ───",
             out->label, out->stage, (unsigned)total_seen,
             s_prev_valid ? "yes" : "no");

    return ESP_OK;
}

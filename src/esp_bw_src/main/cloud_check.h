#pragma once
// ─── Cloud-check false-trigger filter (ESP32-S3 port) ────────────────────────
// Receives pre-decoded YUV tile means, runs EMA z-score background model (3
// photo-buckets × 1 scene-bucket) and burst pre-filter, decides "clouds"
// (suppress) or "process" (real event, upload).
//
// Caller is responsible for camera init/capture/deinit and JPEG decoding.
// bw_cc_assess() only touches NVS and telemetry — no camera dependency.
//
// All thresholds are #define constants in cloud_check.c (change code, rebuild).

#include "esp_err.h"
#include <stdint.h>
#include <stdbool.h>

// Grid layout: QQVGA-equivalent tiles over the full JPEG (20×15 = 300 tiles).
#define CC_TILES_X    20
#define CC_TILES_Y    15
#define CC_NUM_TILES  (CC_TILES_X * CC_TILES_Y)   // 300

typedef struct {
    char    label[16];        // "process" or "clouds"
    char    stage[20];        // burst stage or background-model stage
    char    photo_bucket[12]; // "NORMAL" | "BRIGHT" | "LOWLIGHT"
    uint8_t global_mean;      // mean Y over all tiles (0-255)
} bw_cc_result_t;

// Tell the cloud-check module the wakeup source before calling bw_cc_assess().
// Only RTC frames update the background model; PIR frames are evidence-only.
void bw_cc_set_source(bool is_rtc);

// Run the cloud-check pipeline against pre-decoded tile means.
//   tile_y/u/v: CC_NUM_TILES uint8 values each (BT.601, U/V centred at 128).
//   tile_u/tile_v may be NULL for legacy Y-only mode (chroma gates disabled).
// Loads/saves the NVS model; emits all telemetry keys; writes result to *out.
esp_err_t bw_cc_assess(const uint8_t *tile_y, const uint8_t *tile_u,
                       const uint8_t *tile_v, bw_cc_result_t *out);

// Erase the NVS background model (all cc_* keys, including legacy keys).
// Called on firmware update so the first run starts from the same clean
// prior as the Python validator.
void bw_cc_reset(void);

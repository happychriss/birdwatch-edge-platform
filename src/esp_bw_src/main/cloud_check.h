#pragma once
// ─── Burst false-trigger filter (ESP32-S3) ───────────────────────────────────
// Receives pre-decoded YUV tile means, compares them to the previous frame, and
// decides "clouds" (suppress) or "process" (real event, upload).
//
// The per-tile background model that used to sit behind this (EMA z-score over
// 3 photo-buckets, stages WARMUP / DARK_BLOB / QUIET / AMBIGUOUS) has been
// REMOVED.  Measured on 87 labelled birds it reached 32% recall at a 10%
// false-positive rate against a requirement of 100% — it could not separate
// birds on this scene.  Suppression now happens before the camera is powered,
// from the clock alone; see presuppress.c.  This module remains as a cheap
// second gate for the cases that genuinely need a decoded frame.
//
// Burst stages: FIRST, BRIGHTNESS_SHIFT, DUPLICATE, BRIGHT_STABLE, DIFFUSE, SAFE, NIGHT.
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
    char    stage[20];        // burst stage
    uint8_t global_mean;      // mean Y over all tiles (0-255)
} bw_cc_result_t;

// Tell the module the wakeup source before calling bw_cc_assess().
// Retained because the burst stages still log it; no model is updated now.
void bw_cc_set_source(bool is_rtc);

// Run the cloud-check pipeline against pre-decoded tile means.
//   tile_y/u/v: CC_NUM_TILES uint8 values each (BT.601, U/V centred at 128).
//   tile_u/tile_v may be NULL for legacy Y-only mode (chroma gates disabled).
// Loads/saves the NVS model; emits all telemetry keys; writes result to *out.
esp_err_t bw_cc_assess(const uint8_t *tile_y, const uint8_t *tile_u,
                       const uint8_t *tile_v, bw_cc_result_t *out);

// Erase all cc_* NVS keys, including those of the retired background model.
// Called on firmware update so a device flashed without a full erase still
// reclaims the ~3.6 KB the old per-bucket model occupied.
void bw_cc_reset(void);

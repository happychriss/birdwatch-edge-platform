#pragma once
// ─── Cloud-check false-trigger filter (ESP32-S3 port) ────────────────────────
// Captures a QQVGA (160×120) grayscale frame, computes per-tile EMA z-scores
// against a background model stored in NVS, and decides "clouds" (suppress) or
// "process" (real event, upload).
//
// Caller must NOT have the camera open — this module manages its own
// init/deinit so a subsequent PHOTO mode starts clean.
//
// All parameters are #define constants in cloud_check.c — change code, not
// runtime config.

#include "esp_err.h"

typedef struct {
    char    label[16];      // "process" or "clouds"
    char    stage[20];      // burst: "FIRST"|"BRIGHTNESS_SHIFT"|"DUPLICATE"|"BRIGHT_STABLE"|"DIFFUSE"|"SAFE"
                            // bg-model: "NIGHT"|"WARMUP"|"DARK_OBJ"|"QUIET"|"SCENE_DRIFT"|"AMBIGUOUS"|"CAM_ERR"
    uint8_t global_mean;    // 0-255 ambient brightness from the QQVGA frame (0 on CAM_ERR)
} bw_cc_result_t;

// Run the cloud-check pipeline.  Captures one QQVGA frame, loads the NVS
// model, runs all stages, updates NVS as appropriate, and returns the
// decision in *out.
//
// On camera error the result is {label="process", stage="CAM_ERR"} so
// the upload always proceeds — misidentification beats a missed bird.
esp_err_t bw_cc_assess(bw_cc_result_t *out);

// Erase the NVS background model (all cc_* keys).  Call on firmware update
// so the first run after flash starts from the same clean prior as the
// Python validator — enabling exact parity from frame 1.
void bw_cc_reset(void);

#pragma once
// ─── Light-change false-trigger filter ───────────────────────────────────────
// Captures a tiny grayscale frame, computes the mean pixel value,
// compares to the value persisted in NVS from the previous wake-up.
// If the change exceeds BW_BRIGHTNESS_THRESHOLD, the trigger was
// likely a sun/cloud event rather than a bird.
//
// Caller is expected to have NOT initialised the camera yet — this
// module manages its own init/deinit so subsequent PHOTO mode starts
// clean.

#include "esp_err.h"
#include <stdbool.h>

typedef struct {
    float current_avg;
    float last_avg;
    float bright_diff;
    bool  is_light_change;   // true → likely false trigger, suppress upload
} bw_light_result_t;

esp_err_t bw_light_check(bw_light_result_t *out);

#pragma once
// ─── Battery sensing via ADC1 (oneshot) ──────────────────────────────────────
// Battery reading averages BW_BATT_SAMPLE_COUNT samples and applies the divider factor.

#include "esp_err.h"

esp_err_t bw_adc_init(void);
void      bw_adc_deinit(void);

// Returns measured battery voltage (after divider) in volts.  Negative on error.
float bw_adc_read_battery_voltage(void);

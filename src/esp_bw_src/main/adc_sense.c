#include "adc_sense.h"
#include "config.h"
#include "debug.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"

static const char *TAG = "ADC";

static adc_oneshot_unit_handle_t s_unit;
static adc_cali_handle_t         s_cali;
static bool                      s_cali_ok;

esp_err_t bw_adc_init(void)
{
    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id  = BW_ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_unit);
    if (err != ESP_OK) return bw_log_err(TAG, "adc_oneshot_new_unit", err);

    adc_oneshot_chan_cfg_t ch_cfg = {
        .atten    = BW_ADC_ATTEN,
        .bitwidth = BW_ADC_BITWIDTH,
    };
    err = adc_oneshot_config_channel(s_unit, BW_ADC_BATTERY_CHANNEL, &ch_cfg);
    if (err != ESP_OK) return bw_log_err(TAG, "adc cfg battery ch", err);

    // ESP32-S3 → curve-fitting calibration scheme.
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id  = BW_ADC_UNIT,
        .atten    = BW_ADC_ATTEN,
        .bitwidth = BW_ADC_BITWIDTH,
    };
    err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali);
    s_cali_ok = (err == ESP_OK);
    if (!s_cali_ok) {
        ESP_LOGW(TAG, "calibration unavailable: %s — falling back to raw counts",
                 esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "ADC1 ready (12-bit, atten=12dB, curve-fit cali)");
    }
    return ESP_OK;
}

void bw_adc_deinit(void)
{
    if (s_cali_ok && s_cali) adc_cali_delete_scheme_curve_fitting(s_cali);
    if (s_unit)              adc_oneshot_del_unit(s_unit);
    s_unit = NULL; s_cali = NULL; s_cali_ok = false;
}

static int read_avg(adc_channel_t ch, int n, int delay_ms)
{
    int raw, acc = 0;
    for (int i = 0; i < n; i++) {
        if (adc_oneshot_read(s_unit, ch, &raw) != ESP_OK) return -1;
        ESP_LOGD(TAG, "  raw[%d] ch=%d = %d", i, ch, raw);
        acc += raw;
        if (delay_ms > 0) vTaskDelay(pdMS_TO_TICKS(delay_ms));
    }
    return acc / n;
}

float bw_adc_read_battery_voltage(void)
{
    ESP_LOGI(TAG, "reading battery voltage (n=%d)", BW_BATT_SAMPLE_COUNT);
    int avg_raw = read_avg(BW_ADC_BATTERY_CHANNEL, BW_BATT_SAMPLE_COUNT, BW_BATT_SAMPLE_DELAY_MS);
    if (avg_raw < 0) {
        ESP_LOGE(TAG, "battery ADC read failed");
        return -1.0f;
    }

    int mv = 0;
    if (s_cali_ok && adc_cali_raw_to_voltage(s_cali, avg_raw, &mv) == ESP_OK) {
        float v_pin    = mv / 1000.0f;
        float v_actual = v_pin * BW_BATT_DIVIDER_FACTOR;
        ESP_LOGI(TAG, "  raw=%d → pin=%.3fV → batt=%.3fV", avg_raw, v_pin, v_actual);
        return v_actual;
    }
    // Fallback without calibration
    float v_pin    = (avg_raw * 3.1f) / 4095.0f;
    float v_actual = v_pin * BW_BATT_DIVIDER_FACTOR;
    ESP_LOGW(TAG, "  uncal raw=%d → pin=%.3fV → batt=%.3fV", avg_raw, v_pin, v_actual);
    return v_actual;
}


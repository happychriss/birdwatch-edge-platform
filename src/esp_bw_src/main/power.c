#include "power.h"
#include "config.h"
#include "debug.h"

#include "driver/gpio.h"
#include "esp_sleep.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"

static const char *TAG = "PWR";

// ─── Cycle deadline watchdog ──────────────────────────────────────────────────

static TimerHandle_t s_deadline;

static void on_deadline(TimerHandle_t t)
{
    ESP_LOGE(TAG, "cycle deadline expired — forcing power release");
    bw_blink(BW_BLINK_ERR_WATCHDOG);
    bw_power_release();
    bw_power_deep_sleep();
}

void bw_watchdog_start(uint32_t deadline_ms)
{
    s_deadline = xTimerCreate("wdt", pdMS_TO_TICKS(deadline_ms),
                              pdFALSE, NULL, on_deadline);
    if (s_deadline) {
        xTimerStart(s_deadline, 0);
        ESP_LOGI(TAG, "watchdog armed (%lu s)", (unsigned long)(deadline_ms / 1000));
    } else {
        ESP_LOGE(TAG, "watchdog timer create failed");
    }
}

void bw_watchdog_stop(void)
{
    if (!s_deadline) return;
    xTimerStop(s_deadline, 0);
    xTimerDelete(s_deadline, 0);
    s_deadline = NULL;
    ESP_LOGI(TAG, "watchdog disarmed");
}

esp_err_t bw_power_init(void)
{
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << BW_PWR_HOLD_GPIO),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&out_cfg);
    if (err != ESP_OK) return bw_log_err(TAG, "gpio_config(pwr_hold)", err);

    // Drive HIGH IMMEDIATELY — self-latch keeps TPS22918 ON past the PIR pulse.
    gpio_set_level(BW_PWR_HOLD_GPIO, 1);
    ESP_LOGI(TAG, "PWR_HOLD HIGH (GPIO%d)", BW_PWR_HOLD_GPIO);
    return ESP_OK;
}

void bw_power_release(void)
{
    ESP_LOGW(TAG, "releasing power hold — board will lose power");
    gpio_set_level(BW_PWR_HOLD_GPIO, 0);
}

void bw_power_deep_sleep(void)
{
    // No wakeup source — wakeup is always a cold boot triggered by TPS22918
    // when the PIR fires.  On battery TPS22918 kills us when PIR drops, so
    // this call only matters on USB (sleeps indefinitely; use BW_DEV_NO_SLEEP).
    ESP_LOGI(TAG, "entering deep sleep");
    esp_deep_sleep_start();
}

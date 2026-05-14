#include "debug.h"
#include "config.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_psram.h"
#include "esp_sleep.h"
#include "esp_app_desc.h"
#include "nvs_flash.h"
#include "nvs.h"

void bw_blink_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BW_LED_BUILTIN_GPIO,
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));
    // XIAO LED is active-low → start OFF (HIGH).
    gpio_set_level(BW_LED_BUILTIN_GPIO, 1);
}

void bw_blink(bw_blink_code_t code)
{
    if (code == BW_BLINK_BOOT) {
        // Boot: 4 rapid blinks (30 ms ON / 70 ms OFF).
        // Fired right after power-hold is asserted so a crash at any later step
        // still leaves a visible trace.  The burst is distinct from the single
        // milestone flash and from error long-blinks.
        for (int i = 0; i < 4; i++) {
            gpio_set_level(BW_LED_BUILTIN_GPIO, 0);
            vTaskDelay(pdMS_TO_TICKS(30));
            gpio_set_level(BW_LED_BUILTIN_GPIO, 1);
            vTaskDelay(pdMS_TO_TICKS(70));
        }
    } else if (code >= 10) {
        // Error: N long blinks wrapped in 800 ms silence on each side.
        int n = code - 10;
        vTaskDelay(pdMS_TO_TICKS(800));
        for (int i = 0; i < n; i++) {
            gpio_set_level(BW_LED_BUILTIN_GPIO, 0);
            vTaskDelay(pdMS_TO_TICKS(500));
            gpio_set_level(BW_LED_BUILTIN_GPIO, 1);
            if (i < n - 1) vTaskDelay(pdMS_TO_TICKS(300));
        }
        vTaskDelay(pdMS_TO_TICKS(800));
    } else {
        // Milestone: exactly 1 short blink (80 ms ON / 120 ms OFF).
        gpio_set_level(BW_LED_BUILTIN_GPIO, 0);
        vTaskDelay(pdMS_TO_TICKS(80));
        gpio_set_level(BW_LED_BUILTIN_GPIO, 1);
        vTaskDelay(pdMS_TO_TICKS(120));
    }
}

void bw_hexdump(const char *tag, const void *buf, size_t len)
{
    ESP_LOG_BUFFER_HEXDUMP(tag, buf, len, ESP_LOG_DEBUG);
}

void bw_log_sysinfo(const char *tag)
{
    esp_chip_info_t info;
    esp_chip_info(&info);

    uint32_t flash_size = 0;
    esp_flash_get_size(NULL, &flash_size);

    size_t free_8bit  = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    size_t total_8bit = heap_caps_get_total_size(MALLOC_CAP_8BIT);
    size_t free_psram = esp_psram_is_initialized()
                            ? heap_caps_get_free_size(MALLOC_CAP_SPIRAM)
                            : 0;
    size_t total_psram = esp_psram_is_initialized() ? esp_psram_get_size() : 0;

    const esp_app_desc_t *app = esp_app_get_description();

    ESP_LOGI(tag, "─── system snapshot ───");
    ESP_LOGI(tag, "  chip: %s rev %d.%d, %d core(s), flash %lu MB",
             CONFIG_IDF_TARGET, info.revision / 100, info.revision % 100,
             info.cores, (unsigned long)(flash_size / (1024 * 1024)));
    ESP_LOGI(tag, "  heap: %u / %u KB free (8-bit)",
             (unsigned)(free_8bit / 1024), (unsigned)(total_8bit / 1024));
    ESP_LOGI(tag, "  psram: %u / %u KB free", (unsigned)(free_psram / 1024),
             (unsigned)(total_psram / 1024));
    ESP_LOGI(tag, "  app: %s v%s, idf %s", app->project_name, app->version,
             app->idf_ver);
    ESP_LOGI(tag, "──────────────────────");
}

void bw_log_wakeup_cause(const char *tag)
{
    uint32_t causes = esp_sleep_get_wakeup_causes();
    if (causes == 0) {
        ESP_LOGI(tag, "wakeup cause: UNDEFINED (cold boot)");
        return;
    }
    static const struct { esp_sleep_source_t src; const char *name; } map[] = {
        { ESP_SLEEP_WAKEUP_EXT0,     "EXT0 (RTC IO)"         },
        { ESP_SLEEP_WAKEUP_EXT1,     "EXT1 (RTC controller)" },
        { ESP_SLEEP_WAKEUP_TIMER,    "TIMER"                  },
        { ESP_SLEEP_WAKEUP_TOUCHPAD, "TOUCHPAD"               },
        { ESP_SLEEP_WAKEUP_ULP,      "ULP"                    },
        { ESP_SLEEP_WAKEUP_GPIO,     "GPIO"                   },
        { ESP_SLEEP_WAKEUP_UART,     "UART"                   },
        { ESP_SLEEP_WAKEUP_WIFI,     "WIFI"                   },
    };
    for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++) {
        if (causes & BIT(map[i].src))
            ESP_LOGI(tag, "wakeup cause: %s (bit %d)", map[i].name, map[i].src);
    }
}

#define BW_NVS_NS    "bwdbg"
#define BW_NVS_STEP  "step"
#define BW_NVS_BOOTS "boots"

static bool s_usb_mode = false;

void bw_checkpoint_set_usb_mode(bool reset_via_usb)
{
    s_usb_mode = reset_via_usb;
}

void bw_checkpoint_write(uint8_t step)
{
    if (s_usb_mode) return;  // preserve battery-run values for post-mortem
    nvs_handle_t h;
    if (nvs_open(BW_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, BW_NVS_STEP, step);
    nvs_commit(h);
    nvs_close(h);
}

void bw_checkpoint_read_log(const char *tag)
{
    nvs_handle_t h;
    if (nvs_open(BW_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;

    uint8_t  last_step = 0;
    uint32_t boots     = 0;
    nvs_get_u8(h,  BW_NVS_STEP,  &last_step);
    nvs_get_u32(h, BW_NVS_BOOTS, &boots);

    boots++;
    nvs_set_u32(h, BW_NVS_BOOTS, boots);
    nvs_commit(h);
    nvs_close(h);

    static const char *step_names[] = {
        "none", "NVS init", "ADC+sensors", "camera captured",
        "WiFi connected", "upload done", "cycle complete",
    };
    const char *name = (last_step < 7) ? step_names[last_step] : "?";
    ESP_LOGI(tag, "boot #%lu | last checkpoint: step %d (%s)",
             (unsigned long)boots, last_step, name);
}

esp_err_t bw_log_err(const char *tag, const char *what, esp_err_t err)
{
    if (err != ESP_OK) {
        ESP_LOGE(tag, "%s failed: %s (0x%x)", what, esp_err_to_name(err), err);
    }
    return err;
}

#include "diag.h"

#include <string.h>
#include <stdio.h>
#include <inttypes.h>

#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "DIAG";

#define NVS_NS   "bw_diag"
#define KEY_LOG  "err_log"
#define KEY_BOOT "boot_cnt"
#define LOG_MAX  480   // comfortably under NVS value size limit; ~8–12 entries

static uint32_t s_boot = 0;
static char     s_log[LOG_MAX + 1];
static size_t   s_len = 0;

void bw_diag_init(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;

    nvs_get_u32(h, KEY_BOOT, &s_boot);
    s_boot++;
    nvs_set_u32(h, KEY_BOOT, s_boot);

    memset(s_log, 0, sizeof(s_log));
    size_t len = LOG_MAX;
    nvs_get_blob(h, KEY_LOG, s_log, &len);
    s_len = strnlen(s_log, LOG_MAX);

    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "boot=%" PRIu32 " pending_errors=%zu chars", s_boot, s_len);
}

void bw_diag_push(const char *msg)
{
    uint32_t total_s = (uint32_t)(esp_timer_get_time() / 1000000ULL);
    uint32_t hh = total_s / 3600;
    uint32_t mm = (total_s % 3600) / 60;
    uint32_t ss = total_s % 60;
    char entry[80];
    int n = snprintf(entry, sizeof(entry), "[%02u:%02u:%02u] %s\n",
                     (unsigned)hh, (unsigned)mm, (unsigned)ss, msg);
    if (n <= 0 || s_len + (size_t)n > LOG_MAX) {
        ESP_LOGW(TAG, "diag log full — dropped: %s", msg);
        return;
    }
    memcpy(s_log + s_len, entry, n);
    s_len += n;
    s_log[s_len] = '\0';

    // Persist immediately — must survive power loss before next WiFi window.
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, KEY_LOG, s_log, s_len);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGW(TAG, "diag: %s", msg);
}

bool bw_diag_has_errors(void)
{
    return s_len > 0;
}

size_t bw_diag_get_log(char *buf, size_t len)
{
    size_t n = s_len < len - 1 ? s_len : len - 1;
    memcpy(buf, s_log, n);
    buf[n] = '\0';
    return n;
}

void bw_diag_clear(void)
{
    memset(s_log, 0, sizeof(s_log));
    s_len = 0;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_key(h, KEY_LOG);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGI(TAG, "diag log cleared");
}

uint32_t bw_diag_boot_count(void)
{
    return s_boot;
}

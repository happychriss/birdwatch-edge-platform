#include "wifi_sta.h"
#include "config.h"
#include "credentials.h"
#include "debug.h"

#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "freertos/timers.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "nvs_flash.h"
#include "nvs.h"

static const char *TAG = "WIFI";

#define BIT_CONNECTED  BIT0
#define BIT_FAIL       BIT1

#define NVS_NAMESPACE   "bw_wifi"
#define NVS_KEY_BSSID   "bssid"
#define NVS_KEY_CHANNEL "channel"

static EventGroupHandle_t    s_evt;
static int                   s_retry;
static esp_netif_t          *s_netif;
static bool                  s_inited;
static bw_wifi_fail_reason_t s_fail_reason = BW_WIFI_FAIL_TIMEOUT;
static TimerHandle_t         s_backoff_timer;

// Deferred retry called by s_backoff_timer — safe to call esp_wifi_connect() from timer task.
static void backoff_cb(TimerHandle_t t)
{
    (void)t;
    esp_wifi_connect();
}

// Transient failures worth retrying — others (wrong password, AP banned MAC) are not.
static bool is_retriable(uint8_t r)
{
    switch (r) {
        case WIFI_REASON_AUTH_EXPIRE:                    // 2
        case WIFI_REASON_DISASSOC_DUE_TO_INACTIVITY:    // 4  AP kicked station (e.g. handshake stall on Fritz!Box)
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:         // 15 (legacy; ESP-IDF uses 204 instead)
        case WIFI_REASON_NO_AP_FOUND:                    // 201
        case WIFI_REASON_AUTH_FAIL:                      // 202
        case WIFI_REASON_ASSOC_FAIL:                     // 203
        case WIFI_REASON_HANDSHAKE_TIMEOUT:              // 204
        case WIFI_REASON_CONNECTION_FAIL:                // 205
            return true;
        default:
            return false;
    }
}

static const char *authmode_str(wifi_auth_mode_t m)
{
    switch (m) {
        case WIFI_AUTH_OPEN:            return "OPEN";
        case WIFI_AUTH_WEP:             return "WEP";
        case WIFI_AUTH_WPA_PSK:         return "WPA";
        case WIFI_AUTH_WPA2_PSK:        return "WPA2";
        case WIFI_AUTH_WPA_WPA2_PSK:    return "WPA/WPA2";
        case WIFI_AUTH_WPA2_ENTERPRISE: return "WPA2-ENT";
        case WIFI_AUTH_WPA3_PSK:        return "WPA3";
        case WIFI_AUTH_WPA2_WPA3_PSK:   return "WPA2/WPA3";
        default:                        return "unknown";
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "STA_START → connecting to '%s'", WIFI_SSID);
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *e = (wifi_event_sta_disconnected_t *)data;
        bool retry = is_retriable(e->reason) && (s_retry < BW_WIFI_MAX_RETRY);
        ESP_LOGW(TAG, "DISCONNECTED  bssid=%02x:%02x:%02x:%02x:%02x:%02x  reason=%d%s  attempt=%d/%d",
                 e->bssid[0], e->bssid[1], e->bssid[2],
                 e->bssid[3], e->bssid[4], e->bssid[5],
                 e->reason, retry ? "" : " [no-retry]",
                 s_retry + 1, BW_WIFI_MAX_RETRY);
        if (retry) {
            s_retry++;
            // All retriable failures — including reason=205 (AP briefly invisible) — go
            // through the backoff timer.  The AP disappears precisely because it just
            // kicked us; it needs recovery time before accepting a new association.
            if (!s_backoff_timer)
                s_backoff_timer = xTimerCreate("wbackoff",
                                      pdMS_TO_TICKS(BW_WIFI_BACKOFF_HARD_MS),
                                      pdFALSE, NULL, backoff_cb);
            xTimerStart(s_backoff_timer, 0);
        } else {
            if (e->reason == WIFI_REASON_NO_AP_FOUND) {
                s_fail_reason = BW_WIFI_FAIL_NOT_FOUND;
            } else if (e->reason == WIFI_REASON_AUTH_FAIL              ||
                       e->reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
                       e->reason == WIFI_REASON_HANDSHAKE_TIMEOUT) {
                s_fail_reason = BW_WIFI_FAIL_AUTH;
            } else {
                s_fail_reason = BW_WIFI_FAIL_TIMEOUT;
            }
            xEventGroupSetBits(s_evt, BIT_FAIL);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "GOT_IP " IPSTR " gw " IPSTR,
                 IP2STR(&e->ip_info.ip), IP2STR(&e->ip_info.gw));
        s_retry = 0;
        xEventGroupSetBits(s_evt, BIT_CONNECTED);
    }
}

// XIAO ESP32-S3: GPIO3 selects antenna — 0=built-in, 1=external U.FL.
static void select_external_antenna(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << GPIO_NUM_3),
        .mode         = GPIO_MODE_OUTPUT,
    };
    gpio_config(&io);
    gpio_set_level(GPIO_NUM_3, 1);
    ESP_LOGI(TAG, "antenna → external  GPIO3=%d", gpio_get_level(GPIO_NUM_3));
}

// ─── NVS connection cache (BSSID + channel) ──────────────────────────────────
// Saves the BSSID and primary channel of the last successful connection.
// Channel is used as a scan hint on the next boot; 0 means scan all channels.
// Both are cleared on connection failure so the next boot rediscovers the AP.

static bool nvs_load_bssid(uint8_t bssid[6])
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return false;
    size_t len = 6;
    bool ok = (nvs_get_blob(h, NVS_KEY_BSSID, bssid, &len) == ESP_OK && len == 6);
    nvs_close(h);
    return ok;
}

static void nvs_save_connection(const uint8_t bssid[6], uint8_t channel)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_blob(h, NVS_KEY_BSSID, bssid, 6);
    nvs_set_u8(h, NVS_KEY_CHANNEL, channel);
    nvs_commit(h);
    nvs_close(h);
}

static void nvs_clear_connection(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_erase_key(h, NVS_KEY_BSSID);
    nvs_erase_key(h, NVS_KEY_CHANNEL);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "NVS connection cache cleared");
}

static uint8_t nvs_load_channel(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return 0;
    uint8_t ch = 0;
    nvs_get_u8(h, NVS_KEY_CHANNEL, &ch);
    nvs_close(h);
    return ch;
}

esp_err_t bw_wifi_init(void)
{
    if (s_inited) return ESP_OK;

    select_external_antenna();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    // No esp_wifi_restore() — preserves NVS PMK cache for faster WPA2 handshake.
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    // Lock to DE channel plan (ch1–13); manual so firmware overrides AP's country IE.
    wifi_country_t country = {
        .cc     = BW_WIFI_COUNTRY_CC,
        .schan  = 1,
        .nchan  = 13,
        .policy = WIFI_COUNTRY_POLICY_MANUAL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_country(&country));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &on_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &on_event, NULL, NULL));

    s_evt = xEventGroupCreate();
    s_inited = true;
    ESP_LOGI(TAG, "WiFi STA initialised (country=%s)", BW_WIFI_COUNTRY_CC);
    return ESP_OK;
}

// ─── Connect ─────────────────────────────────────────────────────────────────
// Attempts connection with the given BSSID hint (NULL = any).
// On success, saves the connected BSSID to NVS for the next boot.

static esp_err_t try_connect(const uint8_t *bssid)
{
    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid,     WIFI_SSID,   sizeof(wc.sta.ssid));
    strncpy((char *)wc.sta.password, WIFI_PASSWD, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wc.sta.sae_pwe_h2e        = WPA3_SAE_PWE_BOTH;
    wc.sta.pmf_cfg.capable    = true;
    wc.sta.pmf_cfg.required   = false;
    wc.sta.scan_method        = WIFI_FAST_SCAN;
    wc.sta.channel            = nvs_load_channel();  // 0 = scan all if no cache

    if (bssid) {
        memcpy(wc.sta.bssid, bssid, 6);
        wc.sta.bssid_set = 1;
        if (wc.sta.channel)
            ESP_LOGI(TAG, "connect  bssid=%02x:%02x:%02x:%02x:%02x:%02x  ch=%d (cached)",
                     bssid[0], bssid[1], bssid[2], bssid[3], bssid[4], bssid[5],
                     wc.sta.channel);
        else
            ESP_LOGI(TAG, "connect  bssid=%02x:%02x:%02x:%02x:%02x:%02x  ch=scan",
                     bssid[0], bssid[1], bssid[2], bssid[3], bssid[4], bssid[5]);
    } else {
        ESP_LOGI(TAG, "connect (any bssid)  ch=%s",
                 wc.sta.channel ? "cached" : "scan");
    }

    s_retry = 0;
    s_fail_reason = BW_WIFI_FAIL_TIMEOUT;  // default; overwritten by on_event if BIT_FAIL fires
    xEventGroupClearBits(s_evt, BIT_CONNECTED | BIT_FAIL);

    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    esp_err_t err = esp_wifi_start();
    if (err != ESP_OK) return bw_log_err(TAG, "esp_wifi_start", err);

    EventBits_t bits = xEventGroupWaitBits(
        s_evt, BIT_CONNECTED | BIT_FAIL, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(BW_WIFI_TIMEOUT_MS));

    if (bits & BIT_CONNECTED) {
        wifi_ap_record_t ap = {0};
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            ESP_LOGI(TAG, "connected  bssid=%02x:%02x:%02x:%02x:%02x:%02x  rssi=%d  ch=%d  auth=%s",
                     ap.bssid[0], ap.bssid[1], ap.bssid[2],
                     ap.bssid[3], ap.bssid[4], ap.bssid[5],
                     ap.rssi, ap.primary, authmode_str(ap.authmode));
            nvs_save_connection(ap.bssid, ap.primary);
        }
        return ESP_OK;
    }

    // Failed — clear cached channel so next boot scans all channels fresh.
    nvs_clear_connection();
    ESP_LOGW(TAG, "connect failed (bits=0x%lx)", (unsigned long)bits);
    esp_wifi_stop();
    return ESP_FAIL;
}

static bool bssid_is_zero(const uint8_t b[6])
{
    return (b[0] | b[1] | b[2] | b[3] | b[4] | b[5]) == 0;
}

esp_err_t bw_wifi_connect_blocking(void)
{
    if (!s_inited) bw_wifi_init();

    static const uint8_t pinned[6] = BW_WIFI_BSSID;
    if (!bssid_is_zero(pinned)) {
        // Pinned mode — strict: only this BSSID, no scan fallback.
        // Recovery is handled by the reboot-once policy in main.c, not by
        // scanning (which could land us on a mesh extender).
        ESP_LOGI(TAG, "pinned BSSID — strict mode, no scan fallback");
        return try_connect(pinned);
    }

    // Scan mode (BW_WIFI_BSSID = zeros) — cached BSSID first, fall back to scan.
    uint8_t cached_bssid[6];
    if (nvs_load_bssid(cached_bssid)) {
        ESP_LOGI(TAG, "NVS BSSID cached — skipping scan");
        if (try_connect(cached_bssid) == ESP_OK) return ESP_OK;
        ESP_LOGW(TAG, "cached BSSID failed — clearing cache, backing off %d ms",
                 BW_WIFI_BACKOFF_MS);
        nvs_clear_connection();
        vTaskDelay(pdMS_TO_TICKS(BW_WIFI_BACKOFF_MS));
    }
    return try_connect(NULL);
}

esp_err_t bw_wifi_disconnect(void)
{
    if (!s_inited) return ESP_OK;
    esp_wifi_disconnect();
    esp_wifi_stop();
    ESP_LOGI(TAG, "WiFi stopped");
    return ESP_OK;
}

void bw_wifi_get_ip(char *out, size_t out_len)
{
    esp_netif_ip_info_t ip = {0};
    if (s_netif && esp_netif_get_ip_info(s_netif, &ip) == ESP_OK) {
        snprintf(out, out_len, IPSTR, IP2STR(&ip.ip));
    } else {
        snprintf(out, out_len, "0.0.0.0");
    }
}

bw_wifi_fail_reason_t bw_wifi_last_fail_reason(void)
{
    return s_fail_reason;
}

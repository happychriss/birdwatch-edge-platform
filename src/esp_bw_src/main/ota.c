#include "ota.h"
#include "config.h"
#include "power.h"

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"

#include <string.h>

static const char *TAG = "OTA";

// Streamed in chunks so the whole image never has to be held in RAM.
#define OTA_CHUNK  4096


const char *bw_ota_version(void)
{
    const esp_app_desc_t *d = esp_app_get_description();
    return d ? d->version : "?";
}


void bw_ota_sha256_hex(char *out, size_t cap)
{
    if (!out || cap == 0) return;
    out[0] = '\0';
    const esp_app_desc_t *d = esp_app_get_description();
    if (!d || cap < 65) return;
    for (int i = 0; i < 32; i++)
        snprintf(out + i * 2, cap - i * 2, "%02x", d->app_elf_sha256[i]);
}


bool bw_ota_pending_verify(void)
{
    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (!run || esp_ota_get_state_partition(run, &st) != ESP_OK) return false;
    return st == ESP_OTA_IMG_PENDING_VERIFY;
}


void bw_ota_mark_valid(void)
{
    if (!bw_ota_pending_verify()) return;
    if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK)
        ESP_LOGI(TAG, "running image confirmed — rollback cancelled");
    else
        ESP_LOGW(TAG, "could not mark image valid; it will roll back next boot");
}


// GET the server's wanted-image descriptor.  Returns true and fills sha_out on
// success.  Any failure here is non-fatal: we simply keep running what we have.
static bool fetch_wanted_sha(char *sha_out, size_t cap)
{
    esp_http_client_config_t cfg = {
        .url        = BW_OTA_VERSION_URL,
        .method     = HTTP_METHOD_GET,
        .timeout_ms = BW_HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return false;

    bool ok = false;
    char body[256] = {0};
    if (esp_http_client_open(c, 0) == ESP_OK) {
        esp_http_client_fetch_headers(c);
        int code = esp_http_client_get_status_code(c);
        int n = esp_http_client_read(c, body, sizeof(body) - 1);
        if (code == 200 && n > 0) {
            body[n] = '\0';
            cJSON *root = cJSON_ParseWithLength(body, n);
            if (root) {
                cJSON *sha = cJSON_GetObjectItem(root, "sha256");
                if (cJSON_IsString(sha) && strlen(sha->valuestring) >= 32) {
                    snprintf(sha_out, cap, "%.72s", sha->valuestring);
                    ok = true;
                }
                cJSON_Delete(root);
            }
        } else {
            ESP_LOGW(TAG, "version check: status=%d len=%d", code, n);
        }
        esp_http_client_close(c);
    }
    esp_http_client_cleanup(c);
    return ok;
}


esp_err_t bw_ota_check_and_apply(void)
{
    char running[80], wanted[80] = {0};
    bw_ota_sha256_hex(running, sizeof(running));

    if (!fetch_wanted_sha(wanted, sizeof(wanted))) {
        ESP_LOGI(TAG, "no usable version reply — staying on %s", bw_ota_version());
        return ESP_FAIL;
    }
    if (strncmp(running, wanted, 64) == 0) {
        ESP_LOGI(TAG, "up to date (%s)", bw_ota_version());
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "update available: running %.16s… wanted %.16s…", running, wanted);

    const esp_partition_t *dst = esp_ota_get_next_update_partition(NULL);
    if (!dst) {
        ESP_LOGE(TAG, "no OTA slot available");
        return ESP_FAIL;
    }

    esp_http_client_config_t cfg = {
        .url        = BW_OTA_BIN_URL,
        .method     = HTTP_METHOD_GET,
        .timeout_ms = BW_HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return ESP_FAIL;
    if (esp_http_client_open(c, 0) != ESP_OK) {
        esp_http_client_cleanup(c);
        ESP_LOGE(TAG, "cannot open %s", BW_OTA_BIN_URL);
        return ESP_FAIL;
    }
    int total = esp_http_client_fetch_headers(c);
    int code  = esp_http_client_get_status_code(c);
    if (code != 200) {
        ESP_LOGE(TAG, "firmware fetch status=%d", code);
        esp_http_client_close(c); esp_http_client_cleanup(c);
        return ESP_FAIL;
    }

    // The download is the one phase that can plausibly outlast the normal cycle
    // budget, so give the watchdog its own, larger deadline for the duration
    // rather than letting a slow link look like a hung cycle.
    bw_watchdog_stop();
    bw_watchdog_start(BW_OTA_TIMEOUT_MS);

    esp_ota_handle_t h = 0;
    esp_err_t err = esp_ota_begin(dst, OTA_WITH_SEQUENTIAL_WRITES, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(err));
        esp_http_client_close(c); esp_http_client_cleanup(c);
        bw_watchdog_stop(); bw_watchdog_start(BW_CYCLE_TIMEOUT_MS);
        return err;
    }
    ESP_LOGI(TAG, "writing %d bytes to %s", total, dst->label);

    static uint8_t buf[OTA_CHUNK];
    int written = 0;
    while (1) {
        int n = esp_http_client_read(c, (char *)buf, sizeof(buf));
        if (n < 0) { err = ESP_FAIL; break; }
        if (n == 0) break;                       // complete
        err = esp_ota_write(h, buf, n);
        if (err != ESP_OK) break;
        written += n;
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);

    if (err == ESP_OK && total > 0 && written != total) {
        ESP_LOGE(TAG, "short download: %d/%d bytes", written, total);
        err = ESP_ERR_INVALID_SIZE;
    }
    if (err == ESP_OK) err = esp_ota_end(h);      // validates the image
    else               esp_ota_abort(h);

    if (err == ESP_OK) {
        err = esp_ota_set_boot_partition(dst);
        if (err == ESP_OK)
            ESP_LOGI(TAG, "image armed on %s (%d bytes) — active after the next "
                          "natural wake; no reboot here", dst->label, written);
    }
    if (err != ESP_OK)
        ESP_LOGE(TAG, "OTA failed: %s — staying on the current image",
                 esp_err_to_name(err));

    bw_watchdog_stop();
    bw_watchdog_start(BW_CYCLE_TIMEOUT_MS);
    return err;
}

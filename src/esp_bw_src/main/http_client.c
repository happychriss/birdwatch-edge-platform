#include "http_client.h"
#include "config.h"
#include "debug.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"

static const char *TAG = "HTTP";

// ─── Generic event handler — logs everything for deep debugging ────────────
static esp_err_t evt_handler(esp_http_client_event_t *evt)
{
    switch (evt->event_id) {
        case HTTP_EVENT_ERROR:        ESP_LOGW(TAG, "  EVT ERROR");           break;
        case HTTP_EVENT_ON_CONNECTED: ESP_LOGD(TAG, "  EVT CONNECTED");       break;
        case HTTP_EVENT_HEADERS_SENT: ESP_LOGD(TAG, "  EVT HEADERS_SENT");    break;
        case HTTP_EVENT_ON_HEADER:
            ESP_LOGD(TAG, "  EVT HEADER %s: %s", evt->header_key, evt->header_value);
            break;
        case HTTP_EVENT_ON_DATA:
            ESP_LOGD(TAG, "  EVT DATA  len=%d", evt->data_len);
            break;
        case HTTP_EVENT_ON_HEADERS_COMPLETE: ESP_LOGD(TAG, "  EVT HEADERS_COMPLETE"); break;
        case HTTP_EVENT_ON_STATUS_CODE:      ESP_LOGD(TAG, "  EVT STATUS_CODE");     break;
        case HTTP_EVENT_ON_FINISH:    ESP_LOGD(TAG, "  EVT FINISH");          break;
        case HTTP_EVENT_DISCONNECTED: ESP_LOGD(TAG, "  EVT DISCONNECTED");    break;
        case HTTP_EVENT_REDIRECT:     ESP_LOGD(TAG, "  EVT REDIRECT");        break;
    }
    return ESP_OK;
}

// ───────────────────────────── /status POST ────────────────────────────────
esp_err_t bw_http_post_status(float battery_v, const char *msg)
{
    // Use cJSON so any special chars in msg (newlines, quotes) are properly escaped.
    char batt_str[16];
    snprintf(batt_str, sizeof(batt_str), "%.3f", battery_v);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "battery",   batt_str);
    cJSON_AddStringToObject(root, "source",    BW_HTTP_SOURCE);
    cJSON_AddStringToObject(root, "trigger",   msg);
    cJSON_AddStringToObject(root, "brightdiff", "0.0");
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) return ESP_ERR_NO_MEM;

    esp_http_client_config_t cfg = {
        .url           = BW_STATUS_URL,
        .method        = HTTP_METHOD_POST,
        .timeout_ms    = BW_HTTP_TIMEOUT_MS,
        .event_handler = evt_handler,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) { cJSON_free(body); return ESP_FAIL; }

    esp_http_client_set_header(c, "Content-Type", "application/json");
    esp_http_client_set_post_field(c, body, strlen(body));

    ESP_LOGI(TAG, "POST %s  len=%u", BW_STATUS_URL, (unsigned)strlen(body));
    esp_err_t err = esp_http_client_perform(c);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "  → status=%d len=%lld",
                 esp_http_client_get_status_code(c),
                 esp_http_client_get_content_length(c));
    } else {
        ESP_LOGE(TAG, "  perform: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(c);
    cJSON_free(body);
    return err;
}

// ─────────────── multipart writer helpers (image upload) ───────────────────
static esp_err_t write_chunk(esp_http_client_handle_t c, const char *data, size_t len)
{
    int written = esp_http_client_write(c, data, len);
    if (written != (int)len) {
        ESP_LOGE(TAG, "  short write: %d / %u", written, (unsigned)len);
        return ESP_FAIL;
    }
    return ESP_OK;
}

static int parse_global_status(const char *body, int body_len)
{
    cJSON *root = cJSON_ParseWithLength(body, body_len);
    if (!root) {
        ESP_LOGE(TAG, "  cJSON parse failed");
        return BW_MODE_ERROR;
    }
    cJSON *gs = cJSON_GetObjectItem(root, "global_status");
    int    rc = BW_MODE_ERROR;
    if (cJSON_IsString(gs)) {
        ESP_LOGI(TAG, "  server global_status='%s'", gs->valuestring);
        if (strcmp(gs->valuestring, "PIR_Sensor")    == 0) rc = BW_MODE_PIR_SENSOR;
        else if (strcmp(gs->valuestring, "Camera_Server") == 0) rc = BW_MODE_CAMERA_SERVER;
    } else {
        ESP_LOGE(TAG, "  no global_status key in reply");
    }
    cJSON_Delete(root);
    return rc;
}

bw_mode_t bw_http_upload_image(const char    *meta_json,
                               const uint8_t *jpg_buf,
                               size_t         jpg_len)
{
    static const char *boundary = "----BWBoundary7MA4YWxkTrZu0gW";
    const char *meta = (meta_json && meta_json[0]) ? meta_json : "{}";

    // Two multipart parts: meta (JSON string) + image (JPEG binary).
    // Adding or removing telemetry keys never changes this builder.
    char head[8192];
    int  hl = snprintf(head, sizeof(head),
        "--%s\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n%s\r\n"
        "--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"image.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n",
        boundary, meta,
        boundary);
    if (hl <= 0 || hl >= (int)sizeof(head)) {
        ESP_LOGE(TAG, "head buffer too small (meta_len=%u)", (unsigned)strlen(meta));
        return BW_MODE_ERROR;
    }

    char tail[64];
    int  tl = snprintf(tail, sizeof(tail), "\r\n--%s--\r\n", boundary);
    int  total = hl + (int)jpg_len + tl;

    char ct[96];
    snprintf(ct, sizeof(ct), "multipart/form-data; boundary=%s", boundary);

    char img_len_str[16], total_len_str[16];
    snprintf(img_len_str,   sizeof(img_len_str),   "%u",  (unsigned)jpg_len);
    snprintf(total_len_str, sizeof(total_len_str), "%d",  total);

    ESP_LOGI(TAG, "POST %s   img=%u total=%d", BW_UPLOAD_URL,
             (unsigned)jpg_len, total);

    esp_http_client_config_t cfg = {
        .url           = BW_UPLOAD_URL,
        .method        = HTTP_METHOD_POST,
        .timeout_ms    = BW_HTTP_TIMEOUT_MS,
        .event_handler = evt_handler,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return BW_MODE_ERROR;

    esp_http_client_set_header(c, "Content-Type",   ct);
    esp_http_client_set_header(c, "Content-Length", total_len_str);
    esp_http_client_set_header(c, "Image-Length",   img_len_str);

    esp_err_t err = esp_http_client_open(c, total);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "  open: %s", esp_err_to_name(err));
        esp_http_client_cleanup(c);
        return BW_MODE_ERROR;
    }

    if (write_chunk(c, head, hl)                  != ESP_OK ||
        write_chunk(c, (const char *)jpg_buf, jpg_len) != ESP_OK ||
        write_chunk(c, tail, tl)                  != ESP_OK) {
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
        return BW_MODE_ERROR;
    }

    int hdrs = esp_http_client_fetch_headers(c);
    int code = esp_http_client_get_status_code(c);
    ESP_LOGI(TAG, "  → status=%d content_len=%d", code, hdrs);

    char  reply[512];
    int   off  = 0;
    while (off < (int)sizeof(reply) - 1) {
        int r = esp_http_client_read(c, reply + off, sizeof(reply) - 1 - off);
        if (r <= 0) break;
        off += r;
    }
    reply[off] = '\0';

    esp_http_client_close(c);
    esp_http_client_cleanup(c);

    if (code != 200 || off == 0) {
        ESP_LOGE(TAG, "  upload failed (status=%d, body_len=%d)", code, off);
        return BW_MODE_ERROR;
    }
    ESP_LOGI(TAG, "  reply: %s", reply);

    return (bw_mode_t)parse_global_status(reply, off);
}

#include "camera_server.h"
#include "config.h"
#include "debug.h"

#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "esp_http_server.h"
#include "esp_camera.h"

static const char *TAG = "CAMSRV";

#define BOUNDARY  "BWMJPEGBOUNDARY"
static const char *STREAM_CT      = "multipart/x-mixed-replace;boundary=" BOUNDARY;
static const char *STREAM_BOUNDARY = "\r\n--" BOUNDARY "\r\n";
static const char *STREAM_PART    = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static httpd_handle_t s_httpd;
static bool           s_stop_flag;

// ─── /  ─── tiny status page so a browser sees something ───────────────────
static esp_err_t handler_index(httpd_req_t *req)
{
    static const char html[] =
        "<!doctype html><html><body>"
        "<h2>BirdWatch camera server</h2>"
        "<p><a href=\"/capture\">/capture</a> – single shot</p>"
        "<p><img src=\"/stream\" style=\"max-width:90%\"/></p>"
        "<p><a href=\"/stop\">/stop</a> – release server</p>"
        "</body></html>";
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, html, HTTPD_RESP_USE_STRLEN);
}

// ─── /capture ──────────────────────────────────────────────────────────────
static esp_err_t handler_capture(httpd_req_t *req)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGE(TAG, "/capture: fb_get failed");
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
    esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    ESP_LOGI(TAG, "/capture → %u bytes", (unsigned)fb->len);
    return res;
}

// ─── /stream — MJPEG ───────────────────────────────────────────────────────
static esp_err_t handler_stream(httpd_req_t *req)
{
    esp_err_t res = httpd_resp_set_type(req, STREAM_CT);
    if (res != ESP_OK) return res;
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

    char part[64];
    while (!s_stop_flag) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
            ESP_LOGW(TAG, "/stream: fb_get failed — abort frame");
            res = ESP_FAIL;
            break;
        }
        size_t hl = snprintf(part, sizeof(part), STREAM_PART, (unsigned)fb->len);

        if ((res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY))) != ESP_OK ||
            (res = httpd_resp_send_chunk(req, part, hl))                                != ESP_OK ||
            (res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len))          != ESP_OK) {
            esp_camera_fb_return(fb);
            ESP_LOGW(TAG, "/stream: client gone");
            break;
        }
        esp_camera_fb_return(fb);
    }
    return res;
}

// ─── /stop ─────────────────────────────────────────────────────────────────
static esp_err_t handler_stop(httpd_req_t *req)
{
    ESP_LOGW(TAG, "/stop received");
    s_stop_flag = true;
    httpd_resp_sendstr(req, "stopping\n");
    return ESP_OK;
}

esp_err_t bw_camera_server_start(void)
{
    if (s_httpd) {
        ESP_LOGW(TAG, "already running");
        return ESP_OK;
    }
    s_stop_flag = false;

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port    = 80;
    cfg.max_uri_handlers = 8;
    cfg.stack_size     = 8192;

    esp_err_t err = httpd_start(&s_httpd, &cfg);
    if (err != ESP_OK) return bw_log_err(TAG, "httpd_start", err);

    httpd_uri_t uris[] = {
        { .uri = "/",        .method = HTTP_GET, .handler = handler_index   },
        { .uri = "/capture", .method = HTTP_GET, .handler = handler_capture },
        { .uri = "/stream",  .method = HTTP_GET, .handler = handler_stream  },
        { .uri = "/stop",    .method = HTTP_GET, .handler = handler_stop    },
    };
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        httpd_register_uri_handler(s_httpd, &uris[i]);
    }

    ESP_LOGI(TAG, "camera HTTP server up on port %d", cfg.server_port);
    return ESP_OK;
}

esp_err_t bw_camera_server_stop(void)
{
    if (!s_httpd) return ESP_OK;
    httpd_stop(s_httpd);
    s_httpd = NULL;
    ESP_LOGI(TAG, "camera HTTP server stopped");
    return ESP_OK;
}

bool bw_camera_server_should_stop(void)
{
    return s_stop_flag;
}

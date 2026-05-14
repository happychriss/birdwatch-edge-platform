#include "light_check.h"
#include "camera.h"
#include "config.h"
#include "debug.h"

#include <math.h>
#include <string.h>
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG       = "LIGHT";
static const char *NVS_NS    = "bw";
static const char *NVS_KEY   = "last_avg";

static esp_err_t read_last(float *out)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "nvs_open(read): %s", esp_err_to_name(err));
        *out = 0.0f;
        return err;
    }
    size_t sz = sizeof(*out);
    err = nvs_get_blob(h, NVS_KEY, out, &sz);
    nvs_close(h);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "no previous brightness in NVS — using 0.0");
        *out = 0.0f;
    }
    return ESP_OK;
}

static esp_err_t write_last(float v)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return bw_log_err(TAG, "nvs_open(write)", err);
    err = nvs_set_blob(h, NVS_KEY, &v, sizeof(v));
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return bw_log_err(TAG, "nvs persist last_avg", err);
}

esp_err_t bw_light_check(bw_light_result_t *out)
{
    memset(out, 0, sizeof(*out));

    esp_err_t err = bw_cam_init(BW_CAM_MODE_LIGHTCHECK);
    if (err != ESP_OK) return err;

    camera_fb_t *fb = bw_cam_capture();
    if (!fb) {
        bw_cam_deinit();
        return ESP_FAIL;
    }

    uint64_t sum = 0;
    for (size_t i = 0; i < fb->len; i++) sum += fb->buf[i];
    out->current_avg = (float)sum / (float)fb->len;
    bw_cam_capture_return(fb);
    bw_cam_deinit();

    read_last(&out->last_avg);
    out->bright_diff     = fabsf(out->current_avg - out->last_avg);
    out->is_light_change = out->bright_diff > BW_BRIGHTNESS_THRESHOLD;

    ESP_LOGI(TAG, "current=%.2f last=%.2f diff=%.2f threshold=%.2f → %s",
             out->current_avg, out->last_avg, out->bright_diff,
             BW_BRIGHTNESS_THRESHOLD,
             out->is_light_change ? "LIGHT CHANGE (suppress)"
                                  : "stable (send image)");

    write_last(out->current_avg);
    return ESP_OK;
}

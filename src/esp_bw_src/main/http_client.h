#pragma once
// ─── HTTP client to the python BirdWatch server ──────────────────────────────
// Status POST   — small JSON heartbeat.
// Image upload  — multipart/form-data POST with the JPEG payload and metadata.
// Both use esp_http_client (ESP-IDF native, no Arduino).

#include "config.h"
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>

esp_err_t bw_http_post_status(float battery_v, const char *trigger);

// Upload an in-memory JPEG with metadata.  Parses the server reply
// and returns the requested mode (PIR_SENSOR or CAMERA_SERVER).
// Negative return → error.
bw_mode_t bw_http_upload_image(float       battery_v,
                               const char *trigger,
                               float       bright_diff,
                               const uint8_t *jpg_buf,
                               size_t         jpg_len);

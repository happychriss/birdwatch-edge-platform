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
// meta_json: serialised telemetry JSON from bw_tele_json() — NULL or "" treated as "{}".
// Adding new telemetry keys never requires changes here.
bw_mode_t bw_http_upload_image(const char    *meta_json,
                               const uint8_t *jpg_buf,
                               size_t         jpg_len);

// Send one batched (suppressed-event) record to /batch.  Same multipart shape
// as the image upload; the server stores it as a `batched` row rather than a
// normal frame.  Returns ESP_OK only when the server has accepted it, so the
// caller can safely delete its local copy.
esp_err_t bw_http_post_batch(const char *meta_json,
                             const uint8_t *jpg_buf, size_t jpg_len);

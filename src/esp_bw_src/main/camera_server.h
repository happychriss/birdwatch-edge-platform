#pragma once
// ─── Minimal camera HTTP server ──────────────────────────────────────────────
// Started when the python server returns "Camera_Server" mode.  Hosts:
//   GET /          — tiny status page
//   GET /capture   — single JPEG
//   GET /stream    — multipart MJPEG
//   GET /stop      — terminates the server (sets terminate flag)
//
// Run pattern in main:
//   bw_camera_server_start();
//   while (!bw_camera_server_should_stop()) vTaskDelay(...);
//   bw_camera_server_stop();

#include "esp_err.h"
#include <stdbool.h>

esp_err_t bw_camera_server_start(void);
esp_err_t bw_camera_server_stop(void);
bool      bw_camera_server_should_stop(void);

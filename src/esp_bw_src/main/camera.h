#pragma once
// ─── Camera control (OV2640 on XIAO ESP32-S3 Sense) ─────────────────────────
// Two clearly-named modes:
//   PHOTO       — full-quality JPEG, HD frame, AE/AGC/AWB on for outdoor use
//   LIGHTCHECK  — grayscale QQVGA, no WB, fixed gain → cheap brightness probe
//
// Lifecycle:
//   bw_cam_init(BW_CAM_MODE_*);
//   ... bw_cam_capture() / bw_cam_capture_return() ...
//   bw_cam_deinit();

#include "esp_err.h"
#include "esp_camera.h"

typedef enum {
    BW_CAM_MODE_PHOTO          = 0,   // normal daylight
    BW_CAM_MODE_LIGHTCHECK     = 1,   // grayscale brightness probe
    BW_CAM_MODE_PHOTO_LOWLIGHT = 2,   // dusk/dawn: extended AEC, high gain
    BW_CAM_MODE_PHOTO_BRIGHT   = 3,   // full sun: reduced EV, protect sky
} bw_cam_mode_t;

esp_err_t bw_cam_init(bw_cam_mode_t mode);
esp_err_t bw_cam_deinit(void);

// Capture and return a frame buffer — caller MUST call
// bw_cam_capture_return(fb) when done.
camera_fb_t *bw_cam_capture(void);
void         bw_cam_capture_return(camera_fb_t *fb);

// Discard `n` warm-up frames (lets AE/AGC settle).  Useful before
// taking the actual photo to avoid first-frame oddities.
void bw_cam_discard_frames(int n, int delay_ms);

// Switch frame size / pixel format on the fly (for camera-server mode).
esp_err_t bw_cam_set_format(pixformat_t fmt, framesize_t size);

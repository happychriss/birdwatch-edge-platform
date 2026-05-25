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

// Decode a JPEG buffer into per-tile YUV means via TJpgDec ROM decoder.
//   jpeg/len : bytes from the camera frame buffer
//   tile_y/u/v : output arrays, each grid_w*grid_h uint8 elements
//              BT.601 full-range: Y in [0,255], U/V in [0,255] centred at 128
//   grid_w/h : tile grid dimensions (e.g. 20×15 for CC_TILES_X × CC_TILES_Y)
// Returns ESP_OK on success, ESP_FAIL on JPEG decode error, ESP_ERR_NO_MEM
// on allocation failure.  ~200 ms for SXGA at 16 MHz XCLK on ESP32-S3.
esp_err_t bw_cam_jpeg_decode_to_tile_means(
    const uint8_t *jpeg, size_t len,
    uint8_t *tile_y, uint8_t *tile_u, uint8_t *tile_v,
    int grid_w, int grid_h);

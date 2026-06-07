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
    BW_CAM_MODE_PHOTO      = 0,   // single unified daylight profile, fixed WB, ETTR-locked
    BW_CAM_MODE_LIGHTCHECK = 1,   // grayscale brightness probe (camera-server / diagnostics)
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

// ─── ETTR (expose-to-the-right) exposure control ───────────────────────────

// Switch the sensor to manual exposure and apply a specific AEC/AGC pair.
//   aec  : integration register value, clamped to [0, BW_AEC_VALUE_MAX]
//   gain : AGC gain table index (0 = 1×, capped at 30)
// Disables exposure_ctrl / gain_ctrl, then flushes one ring-buffer frame so the
// next bw_cam_capture() is taken entirely under the new settings.
void bw_cam_set_exposure_manual(uint16_t aec, uint8_t gain);

// Split a total exposure budget E (= aec × (gain_idx + 1)) into an AEC/AGC pair,
// preferring integration time (lower noise) and only adding gain once AEC is
// saturated at BW_AEC_VALUE_MAX.
void bw_cam_split_exposure(uint32_t E, uint16_t *aec_out, uint8_t *gain_out);

// Read the exposure the live AEC/AGC settled on during the discard-frame window.
// Returns false if there is no sensor or the settled AEC is 0 (not yet running).
bool bw_cam_get_settled_exposure(uint16_t *aec_out, uint8_t *gain_out);

// ETTR meter + lock.  Call after bw_cam_init(PHOTO) + bw_cam_discard_frames().
// Captures a probe JPEG, measures its luma histogram, and locks manual AEC/AGC so
// that the BW_ETTR_HI_PERCENTILE-th percentile sits near BW_ETTR_HI_TARGET with at
// most BW_ETTR_CLIP_BUDGET_PM (per-mille) clipped highlights — pushing the backlit
// foreground as bright as possible while letting the sky blow out.  Iterates up to
// BW_ETTR_ITERS times.  Writes the locked centre exposure to *aec0 / *gain0.
// Returns ESP_OK, or ESP_FAIL if metering could not run (auto exposure is kept).
esp_err_t bw_cam_meter_ettr_lock(uint16_t *aec0, uint8_t *gain0);

// Histogram helpers (256 luma bins).  `total` is the pixel count (sum of bins).
uint8_t  bw_cam_hist_percentile(const uint32_t hist[256], uint32_t total, int pct);
uint32_t bw_cam_hist_clip_count(const uint32_t hist[256], int from_dn);

// Switch frame size / pixel format on the fly (for camera-server mode).
esp_err_t bw_cam_set_format(pixformat_t fmt, framesize_t size);

// Decode a JPEG buffer into per-tile YUV means via TJpgDec ROM decoder.
//   jpeg/len : bytes from the camera frame buffer
//   tile_y/u/v : output arrays, each grid_w*grid_h uint8 elements
//              BT.601 full-range: Y in [0,255], U/V in [0,255] centred at 128
//   grid_w/h : tile grid dimensions (e.g. 20×15 for CC_TILES_X × CC_TILES_Y)
//   hist256  : optional 256-bin luma histogram accumulated over all pixels
//              (NULL to skip).  Caller zeroes it; the decoder adds counts.
// Returns ESP_OK on success, ESP_FAIL on JPEG decode error, ESP_ERR_NO_MEM
// on allocation failure.  ~200 ms for SXGA at 16 MHz XCLK on ESP32-S3.
esp_err_t bw_cam_jpeg_decode_to_tile_means(
    const uint8_t *jpeg, size_t len,
    uint8_t *tile_y, uint8_t *tile_u, uint8_t *tile_v,
    int grid_w, int grid_h, uint32_t *hist256);

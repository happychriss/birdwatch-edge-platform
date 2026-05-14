#pragma once
// ─── Deep-debug logging helpers ──────────────────────────────────────────────
// Every module uses ESP_LOGx with its own TAG.  These helpers add a
// few extras: status-LED blink codes for visual diagnosis, and a
// hexdump shortcut.  All logging goes through esp_log so it can be
// filtered at runtime via esp_log_level_set.

#include "esp_log.h"
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>

// ─── LED blink protocol (readable in the field — no serial required) ─────────
//
// BOOT — 4 rapid blinks (30 ms ON / 70 ms OFF, total ~400 ms):
//   Fires immediately after bw_power_init() so a crash before any later step
//   is visible in the field.  The burst pattern is unmistakable even in daylight.
//   GPIO5 power-hold is asserted before this blink runs.
//
// MILESTONE — exactly 1 short blink (80 ms ON / 120 ms OFF):
//   CAM_OK      JPEG captured successfully
//   WIFI_OK     WiFi connected, IP obtained
//   UPLOAD_OK   image uploaded to server
//   SLEEP       releasing power / entering deep sleep
//
// ERROR — N long blinks (500 ms ON / 300 ms gap), wrapped in 800 ms silence:
//   1  watchdog    cycle deadline fired (> 150 s stuck cycle)
//   2  cam init    camera driver failed to initialise (OV2640/OV3660 I2C error)
//   3  cam capture camera returned no frame (DMA/buffer failure)
//   4  cam alloc   PSRAM allocation failed for JPEG copy buffer
//   5  wifi no AP  SSID not found — out of range or wrong SSID
//   6  wifi auth   authentication rejected — wrong password or PMKID mismatch
//   7  wifi time   connect timeout or DHCP failed after all retries
//   8  upload      HTTP POST to server failed after all retries
//
typedef enum {
    // Milestone codes — always exactly 1 short blink (value is only an identifier).
    BW_BLINK_BOOT         = 0,
    BW_BLINK_CAM_OK       = 1,
    BW_BLINK_WIFI_OK      = 2,
    BW_BLINK_UPLOAD_OK    = 3,
    BW_BLINK_SLEEP        = 4,

    // Error codes — count the long blinks to identify the fault.
    BW_BLINK_ERR_WATCHDOG       = 10 + 1,   //  1 long — cycle deadline watchdog
    BW_BLINK_ERR_CAM_INIT       = 10 + 2,   //  2 long — camera init failed
    BW_BLINK_ERR_CAM_CAPTURE    = 10 + 3,   //  3 long — camera returned no frame
    BW_BLINK_ERR_CAM_ALLOC      = 10 + 4,   //  4 long — PSRAM alloc failed
    BW_BLINK_ERR_WIFI_NOT_FOUND = 10 + 5,   //  5 long — AP not found
    BW_BLINK_ERR_WIFI_AUTH      = 10 + 6,   //  6 long — auth rejected
    BW_BLINK_ERR_WIFI_TIMEOUT   = 10 + 7,   //  7 long — connect timeout / no IP
    BW_BLINK_ERR_UPLOAD         = 10 + 8,   //  8 long — HTTP upload failed
} bw_blink_code_t;

// Blink the on-board LED according to the protocol above.
void bw_blink(bw_blink_code_t code);

// One-time GPIO setup for the LED pin.  Call once before any bw_blink().
void bw_blink_init(void);

// Hex dump of a buffer at DEBUG level under the given tag.
void bw_hexdump(const char *tag, const void *buf, size_t len);

// Log a one-line snapshot of heap/PSRAM/flash state — call at startup
// and at key decision points to spot leaks or low-mem situations.
void bw_log_sysinfo(const char *tag);

// Log the deep-sleep wake-up cause in human-readable form.
void bw_log_wakeup_cause(const char *tag);

// Convenience: log an esp_err_t without aborting.  Returns the err.
esp_err_t bw_log_err(const char *tag, const char *what, esp_err_t err);


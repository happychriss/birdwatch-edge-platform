#pragma once
// ─── Over-the-air firmware update ────────────────────────────────────────────
//
// The device cannot be flashed remotely: it is power-gated, and /dev/ttyACM0
// only exists during the brief active window.  So updates are PULLED, never
// pushed — there is no way to wake the device on demand and no need for one.
//
// How an update actually lands:
//   1. the device wakes as normal (here: only on an RTC alarm — see below)
//   2. it uploads, then asks the server which image it should be running
//   3. if that differs from the running one, it streams the new image into the
//      INACTIVE OTA slot
//   4. it powers off exactly as usual — NO reboot.  Rebooting would drop GPIO5
//      and the TPS22918 would cut power mid-bootloader, which is the whole
//      reason bw_power_reboot_safe() exists.  Nothing here needs that.
//   5. the next natural wake is a cold boot, and the bootloader starts the new
//      slot.  It is a POWERON reset, so the reboot-chain guard in main.c is
//      untouched too.
//
// Only RTC wakes check for updates.  A PIR wake may have a bird waiting, and
// delaying that upload by a ~1.2 MB download to save at most two hours of
// update latency is a bad trade.
//
// SAFETY — rollback.  With CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE the new image
// boots in pending-verify state and the bootloader reverts to the previous slot
// unless the image marks itself valid.  On a normally-on device "prove
// yourself" is vague; here the device power-cycles after every event, so it
// means exactly one cycle.  bw_ota_mark_valid() is therefore called only after
// a full successful upload — firmware that cannot reach the server rolls itself
// back without anyone touching the device.

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>

// Identity of the running image.  `version` is the git describe string ESP-IDF
// embeds automatically (e.g. "c492dbd"); the sha256 of the app ELF is what the
// server actually compares against, since it is exact per build.
const char *bw_ota_version(void);
void        bw_ota_sha256_hex(char *out, size_t cap);

// True when the running image booted from OTA and has not yet been confirmed.
bool bw_ota_pending_verify(void);

// Confirm the running image.  Call ONLY after a cycle that genuinely worked —
// this is what cancels the pending rollback.
void bw_ota_mark_valid(void);

// Ask the server what it wants us running and, if it differs, download it into
// the inactive slot.  Never reboots.  Returns ESP_OK when a new image was
// written and armed, ESP_ERR_NOT_FOUND when already up to date, or an error.
// Requires an established WiFi connection.
esp_err_t bw_ota_check_and_apply(void);

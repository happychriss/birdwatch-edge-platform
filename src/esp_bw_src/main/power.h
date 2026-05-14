#pragma once
// ─── Power control ────────────────────────────────────────────────────────────
//
// WAKEUP / POWER CYCLE ARCHITECTURE
// ──────────────────────────────────
// The XIAO is powered by a TPS22918 load switch controlled by a diode-OR gate:
//   PIR OUT  ──|>── TPS22918 ON   (hardware wakeup line, always wired)
//   GPIO5    ──|>── TPS22918 ON   (firmware self-latch)
//
// Every wakeup is a COLD BOOT — not a deep sleep resume.  Sequence:
//   1. PIR detects motion → TPS22918 ON → board powers up from zero.
//   2. bw_power_init() drives GPIO5 HIGH immediately (self-latch), so
//      the board stays on regardless of what the PIR does next.
//   3. Normal cycle runs (capture, WiFi, upload).
//   4. bw_power_release() drives GPIO5 LOW.
//        • If PIR is already LOW: TPS22918 OFF → board dies instantly.
//          Deep sleep is never reached.
//        • If PIR is still HIGH: TPS22918 stays on via PIR diode.
//          bw_power_deep_sleep() runs — board sleeps with no wakeup source.
//          When PIR eventually drops, TPS22918 cuts power and the board dies.
//   5. Next PIR trigger → step 1 again (always a cold boot).
//
// GPIO1/D0 (PIR state read) is NOT wired.  Wakeup is purely hardware-driven
// by TPS22918; no EXT1 deep sleep wakeup is configured.
//
// For USB/bench development use BW_DEV_NO_SLEEP in config.h — it bypasses
// the release+sleep path and loops the cycle forever over USB power.
//
// Call bw_power_init() FIRST in app_main().  A crash before it means the
// self-latch never asserts; once the PIR pulse drops, the board loses power.

#include "esp_err.h"

// Configure the power-hold GPIO and immediately drive it HIGH.
// Must be the first call in app_main().
esp_err_t bw_power_init(void);

// Release the power hold — TPS22918 cuts power within microseconds if PIR
// is also LOW.  Code after this call may not execute on battery.
void bw_power_release(void);

// Enter deep sleep with no wakeup source.  Call after bw_power_release().
// On battery: TPS22918 kills the board when PIR drops — never returns.
// On USB: sleeps indefinitely; use BW_DEV_NO_SLEEP for bench work instead.
void bw_power_deep_sleep(void);

// Software reset while keeping GPIO5 HIGH via the RTC domain so TPS22918 does
// not cut power during the reboot.  Use when a clean retry is wanted without
// risk of losing power.  bw_power_init() re-takes GPIO5 on the next boot.
// Only call this once per PIR event — use esp_reset_reason() == ESP_RST_SW
// to detect a reboot cycle and avoid looping indefinitely.
void bw_power_reboot_safe(void);

// Cycle deadline watchdog.  Start once per cycle right after bw_power_init();
// disarm with bw_watchdog_stop() on normal completion.  If the cycle has not
// finished within deadline_ms the watchdog fires bw_power_release() and deep
// sleep, guaranteeing the board always powers off.
void bw_watchdog_start(uint32_t deadline_ms);
void bw_watchdog_stop(void);

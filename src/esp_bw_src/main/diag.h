#pragma once
// ─── Persistent error log (NVS-backed) ───────────────────────────────────────
// Accumulates error events across cold boots in NVS.  On the first successful
// WiFi connection, the caller reads the log, posts it to the server, then
// clears it.  Nothing is reported on clean runs.
//
// Usage:
//   1. bw_diag_init()        — call once after nvs_flash_init()
//   2. bw_diag_push("MSG")   — call at every error path; persists to NVS immediately
//   3. After WiFi up:
//        if (bw_diag_has_errors()) {
//            char buf[512];
//            bw_diag_get_log(buf, sizeof(buf));
//            bw_http_post_status(battery_v, buf);
//            bw_diag_clear();
//        }
//
// Each entry is tagged with the current boot counter so you can tell how many
// consecutive failed cycles preceded the successful one.

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

// Load log from NVS and increment the boot counter.
void     bw_diag_init(void);

// Append "[B:N] msg" entry.  Writes to NVS immediately so it survives power loss.
// Silently drops the entry if the log is full (LOG_MAX bytes).
void     bw_diag_push(const char *msg);

// True if any entries are pending.
bool     bw_diag_has_errors(void);

// Copy accumulated log into buf (null-terminated).  Returns chars written.
size_t   bw_diag_get_log(char *buf, size_t len);

// Erase the NVS entry.  Call after successfully posting to the server.
void     bw_diag_clear(void);

// Current boot count (incremented by bw_diag_init on every cold boot).
uint32_t bw_diag_boot_count(void);

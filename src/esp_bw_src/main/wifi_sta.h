#pragma once
// ─── WiFi station ────────────────────────────────────────────────────────────
// Synchronous wrapper around event-driven esp_wifi.
//
// bw_wifi_connect_blocking() has two modes selected by BW_WIFI_BSSID in
// config.h:
//
//   • Pinned mode (BSSID set):  one try_connect(pinned_bssid).  No scan,
//     no fallback.  Avoids mesh extenders entirely.  Recovery is handled
//     by the reboot-once policy in main.c.
//   • Scan mode  (BSSID = 0):   two-stage —
//        1. cached BSSID from NVS (if any)
//        2. scan + connect
//     Falls through with a brief BW_WIFI_BACKOFF_MS pause between stages.
//
// Each try_connect():
//   - retries internally on transient reasons up to BW_WIFI_MAX_RETRY
//   - blocks up to BW_WIFI_TIMEOUT_MS overall (timeout covers ALL retries)
//   - returns ESP_OK once IP_EVENT_STA_GOT_IP fires

#include "esp_err.h"

esp_err_t bw_wifi_init(void);
esp_err_t bw_wifi_connect_blocking(void);
esp_err_t bw_wifi_disconnect(void);

// Get the current STA IP as a dotted string.  Returns "0.0.0.0" if
// not connected.  Caller passes a buffer of at least 16 bytes.
void bw_wifi_get_ip(char *out, size_t out_len);

// ─── Failure reason ──────────────────────────────────────────────────────────
// Valid after bw_wifi_connect_blocking() returns ESP_FAIL.
typedef enum {
    BW_WIFI_FAIL_NOT_FOUND = 0,  // SSID not seen — AP out of range or wrong SSID
    BW_WIFI_FAIL_AUTH,            // association or 4-way handshake rejected
    BW_WIFI_FAIL_TIMEOUT,         // overall connect timeout or DHCP failure
} bw_wifi_fail_reason_t;

bw_wifi_fail_reason_t bw_wifi_last_fail_reason(void);

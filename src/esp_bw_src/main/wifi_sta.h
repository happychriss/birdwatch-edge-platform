#pragma once
// ─── WiFi station ────────────────────────────────────────────────────────────
// Simple synchronous wrapper around event-driven esp_wifi.  Connects
// using credentials.h, retries up to BW_WIFI_MAX_RETRY, and waits up
// to BW_WIFI_TIMEOUT_MS for IP.  Returns ESP_OK on success.

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

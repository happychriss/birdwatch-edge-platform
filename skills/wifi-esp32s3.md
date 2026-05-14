---
name: wifi-esp32s3
description: ESP-IDF WiFi STA setup for XIAO ESP32-S3 — antenna selection, BSSID pinning, retry logic, reboot-once recovery
---

# WiFi STA — ESP32-S3 (ESP-IDF v6)

## Antenna selection (XIAO ESP32-S3 Sense)

GPIO3 controls the RF switch between the built-in PCB trace antenna and the external U.FL connector.
Set it **before** `esp_wifi_init()`:

```c
#include "driver/gpio.h"
gpio_config_t io = { .pin_bit_mask = (1ULL << GPIO_NUM_3), .mode = GPIO_MODE_OUTPUT };
gpio_config(&io);
gpio_set_level(GPIO_NUM_3, 1);  // 1 = external U.FL, 0 = built-in
```

## Init sequence

```c
esp_wifi_init(&cfg);
// Do NOT call esp_wifi_restore() — preserves NVS PMK cache for faster WPA2 handshake.
esp_wifi_set_mode(WIFI_MODE_STA);

wifi_country_t country = { .cc = "DE", .schan = 1, .nchan = 13, .policy = WIFI_COUNTRY_POLICY_MANUAL };
esp_wifi_set_country(&country);  // must be called after init, before start
```

`WIFI_COUNTRY_POLICY_MANUAL` prevents the AP's country IE from overriding the channel plan.

## Connection config

```c
wifi_config_t wc = {0};
strncpy((char *)wc.sta.ssid,     WIFI_SSID,   sizeof(wc.sta.ssid));
strncpy((char *)wc.sta.password, WIFI_PASSWD, sizeof(wc.sta.password));
wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;   // reject WEP/open APs
wc.sta.sae_pwe_h2e        = WPA3_SAE_PWE_BOTH;    // required for WPA2/WPA3 mixed APs
wc.sta.pmf_cfg.capable    = true;
wc.sta.pmf_cfg.required   = false;
wc.sta.scan_method        = WIFI_FAST_SCAN;
wc.sta.channel            = 1;                     // Fritz!Box 2.4 GHz fixed channel
```

**`WPA3_SAE_PWE_BOTH` is mandatory** for any AP in WPA2/WPA3 transition mode.
`HUNT_AND_PECK` alone will fail with reason=2 on those APs.

## Power save

Disable PS before connect — power save during association causes `AUTH_EXPIRE` / `ASSOC_EXPIRE`
because handshake frames are missed during sleep windows:

```c
esp_wifi_set_ps(WIFI_PS_NONE);   // before esp_wifi_start()
```

## BSSID pinning — eliminate mesh roaming (CRITICAL)

**Root cause of persistent WiFi failures in mesh networks:** A Fritz!Box + mesh repeater share
the same SSID. The ESP32 may connect to either BSSID. Repeaters can have different auth
behaviour, different channels, or more fragile 4-way handshake timing. Result: `AUTH_FAIL`
(reason=202) or `4WAY_HANDSHAKE_TIMEOUT` (reason=15) on every attempt.

**Solution: pin to the primary router BSSID.** No scan, no roaming, no surprises.

In `config.h`:
```c
// MAC from Fritzbox UI: Home Network → Network → Network Connections
// Set all bytes to 0 to disable pinning and use scan-based connect instead.
#define BW_WIFI_BSSID  { 0xb4, 0xfc, 0x7d, 0x92, 0xd4, 0x90 }
```

In `wifi_sta.c`:
```c
esp_err_t bw_wifi_connect_blocking(void)
{
    static const uint8_t pinned[6] = BW_WIFI_BSSID;
    if (!bssid_is_zero(pinned)) {
        // Strict: only this BSSID, no scan fallback.
        // If this fails, reboot-once policy in main.c handles recovery.
        return try_connect(pinned);
    }
    // Scan mode (BSSID = zeros): NVS cached BSSID first, then any-BSSID.
    ...
}
```

**Scan mode fallback (BW_WIFI_BSSID = zeros):** NVS cached BSSID → scan. Cache is cleared on
miss so the next boot after a failed cached-BSSID attempt goes straight to scan.

## Retry logic — reason-based filtering

Not all disconnect reasons are worth retrying. Non-retriable reasons (wrong password,
AP MAC-blocked the device) should abort immediately:

```c
static bool is_retriable(uint8_t r) {
    switch (r) {
        case WIFI_REASON_AUTH_EXPIRE:             // 2
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:  // 15
        case WIFI_REASON_NO_AP_FOUND:             // 201
        case WIFI_REASON_AUTH_FAIL:               // 202
        case WIFI_REASON_ASSOC_FAIL:              // 203
        case WIFI_REASON_HANDSHAKE_TIMEOUT:       // 204
        case WIFI_REASON_CONNECTION_FAIL:         // 205
            return true;
        default: return false;   // e.g. reason=3 AUTH_LEAVE = AP kicked us
    }
}
```

`BW_WIFI_MAX_RETRY = 4` (5 total attempts). `BW_WIFI_TIMEOUT_MS = 10000` covers ALL retries
within one `try_connect()` call, not per retry.

Always log the `bssid` field from the disconnect event — it identifies which AP dropped the
connection when multiple APs share an SSID.

## Reboot-once recovery (cold-boot only)

A single soft reboot resolves transient driver hangs better than retrying within the same boot.
Use the RTC GPIO domain to hold GPIO5 HIGH across the reboot so TPS22918 doesn't cut power
during the ~500ms bootloader window:

```c
// power.c
void bw_power_reboot_safe(void)
{
    rtc_gpio_init(BW_PWR_HOLD_GPIO);
    rtc_gpio_set_direction(BW_PWR_HOLD_GPIO, RTC_GPIO_MODE_OUTPUT_ONLY);
    rtc_gpio_set_level(BW_PWR_HOLD_GPIO, 1);
    esp_restart();
}
```

Guard in `main.c` — only reboot on a clean cold boot, not after a previous SW/panic/WDT reboot:
```c
if (esp_reset_reason() == ESP_RST_POWERON)
    bw_power_reboot_safe();
// else: fall through → power off (bounds the chain to exactly one reboot)
```

## Timing budget (worst case)

| Phase | Time |
|-------|------|
| First `try_connect()` | ≤ 10 s |
| Soft reboot | ~0.5 s |
| Second `try_connect()` (after reboot) | ≤ 10 s |
| HTTP retries (3 × 20 s) | ≤ 60 s |
| **Total** | **≤ 80.5 s** |

Watchdog deadline: `BW_CYCLE_TIMEOUT_MS = 150 s` — safe margin.

## Backoff between scan-mode stages

Add `BW_WIFI_BACKOFF_MS = 500` pause between the cached-BSSID attempt and the scan fallback.
Prevents hammering the AP with rapid reconnect attempts on congested channels.

## NVS BSSID cache

Namespace `bw_wifi`, key `bssid` (6-byte blob). Saved on every successful connect.
Cleared when a cached-BSSID attempt fails — ensures the next boot (after reboot) doesn't
waste another 10 s on a stale BSSID before scanning.

## Fritz!Box MAC-block

Fritz!Box 7690 silently blocks a device MAC after repeated failed auth attempts
(brute-force protection). No UI entry — clears on router reboot only. Symptom: reason=2
on every attempt, ~1 s per attempt, no IP ever reached. BSSID pinning eliminates this
by avoiding the mesh repeater that may have triggered the block.

## Key reason codes

| Code | Name | Meaning |
|------|------|---------|
| 2 | AUTH_EXPIRE | AP timed out waiting for auth response |
| 3 | AUTH_LEAVE | AP actively deauthed us |
| 15 | 4WAY_HANDSHAKE_TIMEOUT | EAPOL 4-way stalled |
| 201 | NO_AP_FOUND | AP not visible in connect-scan |
| 202 | AUTH_FAIL | Internal auth failure |
| 203 | ASSOC_FAIL | Association rejected |
| 204 | HANDSHAKE_TIMEOUT | EAPOL timeout (driver-level) |
| 205 | CONNECTION_FAIL | Generic (includes brute-force block) |

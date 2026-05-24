---
name: wifi-esp32s3
description: ESP-IDF WiFi STA setup for XIAO ESP32-S3 — antenna selection, BSSID pinning, dynamic channel cache, Fritz!Box handshake backoff, retry logic, reboot-once recovery
---

# WiFi STA — ESP32-S3 (ESP-IDF v6) with Fritz!Box

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
wc.sta.channel            = nvs_load_channel();   // 0 = scan all; cached on success
```

**`WPA3_SAE_PWE_BOTH` is mandatory** for any AP in WPA2/WPA3 transition mode.
`HUNT_AND_PECK` alone will fail with reason=2 on those APs.

**Do NOT hardcode a channel.** Fritz!Box reports its channel in the log, but firmware-side
configuration and actual broadcast channel can differ. Use the NVS cache pattern below —
on first boot channel=0 triggers a scan, the actual channel is saved on success, and
subsequent boots use the cached value as a fast-connect hint.

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

**Pinned mode is strict — no scan fallback.** Recovery relies on the reboot-once mechanism,
not on trying a different BSSID. This prevents ever accidentally landing on the mesh repeater.

## NVS channel + BSSID cache

Namespace `bw_wifi`, keys `bssid` (6-byte blob) and `channel` (u8). Saved together on every
successful connect using `esp_wifi_sta_get_ap_info()` after `IP_EVENT_STA_GOT_IP`.

- First boot: channel=0 (scan all channels), BSSID pinned or open scan
- Successful connect: saves `ap.primary` channel + actual BSSID
- Next boot: `nvs_load_channel()` returns cached channel → fast connect hint
- Any failure: `nvs_clear_connection()` erases both keys → next boot scans all channels fresh

```c
// On success:
wifi_ap_record_t ap = {0};
esp_wifi_sta_get_ap_info(&ap);
nvs_save_connection(ap.bssid, ap.primary);  // save actual channel, not configured channel

// On any failure:
nvs_clear_connection();
```

Never hardcode the channel in firmware — the AP's actual broadcast channel can differ from
what the router UI shows or from the country setting.

## Fritz!Box handshake ban — backoff timer (CRITICAL)

**Observed pattern:** Fritz!Box temporarily bans a station MAC after ~3 rapid failed
4-way handshake attempts. Symptoms:

```
reason=4  (DISASSOC_DUE_TO_INACTIVITY)  — AP kicks during assoc/early handshake
reason=15 (4WAY_HANDSHAKE_TIMEOUT)      — WPA handshake started but AP stops responding
reason=204 (HANDSHAKE_TIMEOUT)          — same as above, IDF driver-level code
  ...repeated 2-3 times...
reason=202 (AUTH_FAIL)                  — Fritz!Box bans the MAC, auth refused entirely
```

Between each hard failure there is a ~1.5s natural delay (reason=205 retry that can't find
the AP while it recovers), but this is not enough to lift the ban. The station exhausts all
retries while still banned.

**Fix: 2.5s backoff before retrying after reason=4/15/204.** Since `vTaskDelay()` cannot be
called from the event handler, use a one-shot FreeRTOS timer:

```c
static TimerHandle_t s_backoff_timer;

static void backoff_cb(TimerHandle_t t) { (void)t; esp_wifi_connect(); }

// in on_event disconnect handler:
if (retry) {
    s_retry++;
    bool needs_backoff =
        (e->reason == WIFI_REASON_DISASSOC_DUE_TO_INACTIVITY ||  // 4
         e->reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT     ||  // 15
         e->reason == WIFI_REASON_HANDSHAKE_TIMEOUT);            // 204
    if (needs_backoff) {
        if (!s_backoff_timer)
            s_backoff_timer = xTimerCreate("wbackoff",
                                  pdMS_TO_TICKS(BW_WIFI_BACKOFF_HARD_MS),
                                  pdFALSE, NULL, backoff_cb);
        xTimerStart(s_backoff_timer, 0);
    } else {
        esp_wifi_connect();   // reason=205 etc: retry immediately
    }
}
```

`BW_WIFI_BACKOFF_HARD_MS = 2500` in `config.h`.

With backoff, the pattern becomes: fail → 2.5s → soft miss (205, ~1.5s) → retry → succeed,
giving the Fritz!Box enough time to lift the temporary ban before the next attempt.

## Retry logic — reason-based filtering

```c
static bool is_retriable(uint8_t r) {
    switch (r) {
        case WIFI_REASON_AUTH_EXPIRE:                    // 2
        case WIFI_REASON_DISASSOC_DUE_TO_INACTIVITY:    // 4  — Fritz!Box kicks during slow assoc
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:         // 15 (legacy)
        case WIFI_REASON_NO_AP_FOUND:                    // 201
        case WIFI_REASON_AUTH_FAIL:                      // 202
        case WIFI_REASON_ASSOC_FAIL:                     // 203
        case WIFI_REASON_HANDSHAKE_TIMEOUT:              // 204 (IDF primary)
        case WIFI_REASON_CONNECTION_FAIL:                // 205
            return true;
        default: return false;
    }
}
```

`WIFI_REASON_DISASSOC_DUE_TO_INACTIVITY` (4) must be retriable — Fritz!Box sends it when the
station is too slow to start the 4-way handshake. It is **not** an auth failure; do not
classify it as `BW_WIFI_FAIL_AUTH`.

Always log the `bssid` field from the disconnect event — it identifies which AP dropped the
connection when multiple APs share an SSID.

## DHCP timing

`IP_EVENT_STA_GOT_IP` fires only after DHCP completes. Fritz!Box DHCP can take 2-3s after
the WPA handshake. The `xEventGroupWaitBits()` timeout covers ALL retries within one
`try_connect()` call — it is NOT reset per retry. With 6 retries and backoff, budget ~30-40s:

```c
#define BW_WIFI_MAX_RETRY    6      // 7 total attempts
#define BW_WIFI_TIMEOUT_MS   40000  // covers all retries + backoff time + DHCP
```

Too-tight timeout (10s) was the root cause of "connects to run state but never gets IP" — the
timeout fired while DHCP was still in progress after earlier retries consumed the budget.

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
// else: fall through → power off (bounds the chain to exactly one reboot per PIR event)
```

## Timing budget (worst case)

| Phase | Time |
|-------|------|
| First `try_connect()` — up to 7 attempts with backoff | ≤ 40 s |
| Soft reboot (if first try_connect fails) | ~0.5 s |
| Second `try_connect()` | ≤ 40 s |
| HTTP retries (3 × 20 s) | ≤ 60 s |
| **Total** | **≤ 140.5 s** |

Watchdog deadline: `BW_CYCLE_TIMEOUT_MS = 150 s` — adequate margin.

## Key reason codes

| Code | Name | Meaning | Action |
|------|------|---------|--------|
| 2 | AUTH_EXPIRE | AP timed out waiting for auth response | retry |
| 3 | AUTH_LEAVE | AP actively deauthed us | no retry |
| 4 | DISASSOC_DUE_TO_INACTIVITY | AP kicked during slow assoc | retry + backoff |
| 8 | ASSOC_LEAVE | We initiated disconnect | no retry |
| 15 | 4WAY_HANDSHAKE_TIMEOUT | EAPOL 4-way stalled (legacy code) | retry + backoff |
| 201 | NO_AP_FOUND | AP not visible in connect-scan | retry immediately |
| 202 | AUTH_FAIL | Fritz!Box banned the MAC | no retry (ban must lift) |
| 203 | ASSOC_FAIL | Association rejected | retry |
| 204 | HANDSHAKE_TIMEOUT | EAPOL timeout (IDF driver code) | retry + backoff |
| 205 | CONNECTION_FAIL | Generic / AP briefly invisible after kick | retry immediately |

## Fritz!Box-specific notes

- Fritz!Box + AVM mesh repeaters share the same SSID/password. Always pin the BSSID.
- Fritz!Box broadcasts on the channel it actually uses, which may differ from what the
  UI shows as "configured". Read the channel from `ap.primary` after connect, not from config.
- Fritz!Box implements a short (~seconds) MAC rate-limit after rapid failed handshakes.
  The 2.5s backoff prevents triggering it.
- Fritz!Box DHCP is slower than typical routers — budget 3s after WPA handshake completes.

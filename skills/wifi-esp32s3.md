---
name: wifi-esp32s3
description: ESP-IDF WiFi STA setup for XIAO ESP32-S3 — antenna selection, robustness config, retry logic
---

# WiFi STA — ESP32-S3 (ESP-IDF v5)

## Antenna selection (XIAO ESP32-S3 Sense)

GPIO3 controls the RF switch between the built-in PCB trace antenna and the external U.FL connector.
Set it **before** `esp_wifi_init()`:

```c
#include "driver/gpio.h"
gpio_config_t io = { .pin_bit_mask = (1ULL << GPIO_NUM_3), .mode = GPIO_MODE_OUTPUT };
gpio_config(&io);
gpio_set_level(GPIO_NUM_3, 1);  // 1 = external U.FL, 0 = built-in
```

**Conflict note (BirdWatch):** GPIO3 = ADC_CHANNEL_2 (LDR tap). ADC reads must happen
before `bw_wifi_init()` — the main cycle guarantees this (step 6 ADC, step 8 WiFi).

## Init sequence

```c
esp_wifi_init(&cfg);
esp_wifi_restore();          // clears stale NVS credentials / PMF state
esp_wifi_set_mode(WIFI_MODE_STA);

wifi_country_t country = { .cc = "DE", .schan = 1, .nchan = 13, .policy = WIFI_COUNTRY_POLICY_MANUAL };
esp_wifi_set_country(&country);  // must be called after init, before start
```

`esp_wifi_restore()` is essential after firmware updates or credential changes — without it the
chip can reuse stale PMF or auth state and fail with reason=2 indefinitely.

`esp_wifi_set_country()` with `MANUAL` policy prevents the AP's country IE from overriding
the channel plan; keeps TX behaviour predictable on a German Fritz!Box network.

## Connection config

```c
wifi_config_t wc = {0};
strncpy((char *)wc.sta.ssid,     WIFI_SSID,   sizeof(wc.sta.ssid));
strncpy((char *)wc.sta.password, WIFI_PASSWD, sizeof(wc.sta.password));
wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;   // reject WEP/open APs
wc.sta.sae_pwe_h2e        = WPA3_SAE_PWE_BOTH;    // required for WPA2/WPA3 mixed APs
wc.sta.pmf_cfg.capable    = true;
wc.sta.pmf_cfg.required   = false;
wc.sta.scan_method        = WIFI_FAST_SCAN;        // stops at first BSSID match
wc.sta.channel            = 1;                     // Fritz!Box 2.4 GHz fixed channel
// BSSID lock (use scan-collected or hardcoded):
memcpy(wc.sta.bssid, target_bssid, 6);
wc.sta.bssid_set = 1;
```

**`WPA3_SAE_PWE_BOTH` is mandatory** for Android hotspots and any AP in WPA2/WPA3
transition mode. `HUNT_AND_PECK` will fail with reason=2 on those APs.

**BSSID lock** prevents roaming between two APs with the same SSID (e.g. Fritz!Box +
repeater). The repeater may have different auth behaviour or be on a different channel.

## Power save

Disable PS before connect; re-enable after `GOT_IP` if needed:

```c
esp_wifi_set_ps(WIFI_PS_NONE);   // before esp_wifi_start()
// after stable link:
// esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
```

PS during association causes `AUTH_EXPIRE` / `ASSOC_EXPIRE` because the handshake
frames are missed during sleep windows.

## Retry logic — reason-based filtering

Not all disconnect reasons are worth retrying. Non-retriable reasons (wrong password,
AP MAC-blocked the device) should abort immediately rather than burning all retries:

```c
static bool is_retriable(uint8_t r) {
    switch (r) {
        case WIFI_REASON_AUTH_EXPIRE:             // 2  — timed out before auth completed
        case WIFI_REASON_ASSOC_EXPIRE:            // 4  — association aged out
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:  // 15 — EAPOL stalled
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

In `WIFI_EVENT_STA_DISCONNECTED`:

```c
bool retry = is_retriable(e->reason) && (s_retry < MAX_RETRY);
ESP_LOGW(TAG, "DISCONNECTED bssid=%02x:.. reason=%d%s attempt=%d/%d", ...,
         e->reason, retry ? "" : " [no-retry]", s_retry+1, MAX_RETRY);
if (retry) { s_retry++; esp_wifi_connect(); }
else xEventGroupSetBits(s_evt, BIT_FAIL);
```

Always log the `bssid` field from the disconnect event — it identifies *which* AP dropped
the connection when multiple APs share an SSID.

## Timeout sizing

`BW_WIFI_TIMEOUT_MS = 40000` (40 s). With 5 retries and ~4 s per auth attempt:
5 × 4 s = 20 s + scan time + margin → 40 s is safe. 15 s (old value) was too short.

## Scan-based BSSID discovery

Collect all BSSIDs for the target SSID during a pre-connect scan, then attempt
each one in scan order (strongest RSSI first). Fall back to unloocked connect if
the scan finds no match:

```c
for (int b = 0; b < s_target_count; b++) {
    if (try_one_bssid(s_target_bssid[b], b, s_target_count) == ESP_OK) return ESP_OK;
}
```

## Fritz!Box MAC-block

Fritz!Box 7690 silently blocks a device MAC after repeated failed auth attempts
(brute-force protection). **There is no UI entry for this** — it clears on router reboot
only. Symptom: reason=2 on every attempt, ~1 s per attempt, no IP ever reached.
Confirmed on MAC `74:4d:bd:95:99:98`. A different board (`34:85:18:92:17:80`) connected
immediately to the same AP with identical firmware.

## Key reason codes

| Code | Name | Meaning |
|------|------|---------|
| 2 | AUTH_EXPIRE | AP timed out waiting for auth response |
| 3 | AUTH_LEAVE | AP actively deauthed us |
| 4 | ASSOC_EXPIRE | Association aged out |
| 15 | 4WAY_HANDSHAKE_TIMEOUT | EAPOL 4-way stalled |
| 201 | NO_AP_FOUND | AP not visible in connect-scan |
| 202 | AUTH_FAIL | Internal auth failure |
| 205 | CONNECTION_FAIL | Generic — includes brute-force block |

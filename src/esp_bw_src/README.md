# BirdWatch ESP32-S3 firmware

Pure ESP-IDF (no Arduino). Target: **Seeed XIAO ESP32-S3 Sense** (OV2640).

## Layout

```
esp_bw_src/
├── CMakeLists.txt          top-level project
├── partitions.csv          custom 8 MB flash layout
├── sdkconfig.defaults      PSRAM/USB-CDC/log defaults
└── main/
    ├── CMakeLists.txt
    ├── idf_component.yml   pulls espressif/esp32-camera
    ├── config.h            pins, ADC channels, server URL, thresholds
    ├── credentials.h       WiFi SSID/PASS  (do not commit real values)
    ├── debug.[ch]          ESP_LOG helpers, blink codes, sysinfo
    ├── power.[ch]          self-latch GPIO, watchdog, deep sleep (TPS22918 cold-boot only)
    ├── adc_sense.[ch]      battery + LDR via esp_adc oneshot + curve-fit cali
    ├── wifi_sta.[ch]       event-driven STA, retries, RSSI logging
    ├── camera.[ch]         OV2640 init, PHOTO / LIGHTCHECK modes
    ├── light_check.[ch]    NVS-backed brightness diff false-trigger filter
    ├── http_client.[ch]    /status JSON + /upload multipart, cJSON parser
    ├── camera_server.[ch]  /capture, /stream, /stop, /
    └── main.c              app_main + state machine
```

## Build & flash

Use the project scripts (recommended):
```bash
/workspace/src/scripts/flash_firmware.sh          # build + flash on /dev/ttyACM0
/workspace/src/scripts/monitor.sh                 # serial monitor (resets board)
```

Or directly with ESP-IDF:
```bash
. /home/ubuntu/esp-idf/export.sh
cd /workspace/src/esp_bw_src
idf.py build
idf.py -p /dev/ttyACM0 flash
```

## First-time set-up

1. Edit `main/credentials.h` with real WiFi SSID/password.
2. Verify server IP in `main/config.h` (`BW_SERVER_HOST`).
3. The first build downloads `espressif/esp32-camera` from the
   Component Registry — needs internet.

## WiFi robustness

**BSSID pinning** (configured in `config.h`) eliminates the primary failure mode: mesh
repeaters sharing the same SSID as the Fritz!Box can have different auth timing and cause
`AUTH_FAIL` (reason=202) / `4WAY_HANDSHAKE_TIMEOUT` (reason=15) on every boot.

```c
// config.h — MAC from Fritz!Box UI: Home Network → Network → Network Connections
#define BW_WIFI_BSSID  { 0xb4, 0xfc, 0x7d, 0x92, 0xd4, 0x90 }  // Fritz!Box primary
// Set to { 0,0,0,0,0,0 } to fall back to scan mode (NVS cached BSSID → any-BSSID)
```

**Reboot-once recovery:** on first WiFi failure after a cold boot, `bw_power_reboot_safe()`
holds GPIO5 HIGH via the RTC domain (survives `esp_restart()`) and reboots. The guard
`esp_reset_reason() == ESP_RST_POWERON` ensures only one reboot per PIR event.

**Timing budget:** 10s WiFi + reboot + 10s WiFi + 3×20s HTTP = ≤ 80s, well within the
150s watchdog.

## Debug log levels

All modules log under their own tag (`MAIN`, `PWR`, `ADC`, `WIFI`,
`CAM`, `LIGHT`, `HTTP`, `CAMSRV`, `DBG`). To dial in:

```c
esp_log_level_set("HTTP", ESP_LOG_DEBUG);   // verbose multipart/event trace
esp_log_level_set("ADC",  ESP_LOG_DEBUG);   // per-sample raw values
```

## Visible diagnostics (no serial needed)

| LED pattern | Meaning |
|-------------|---------|
| 4 rapid bursts (30/70 ms) | Boot — fires immediately after self-latch |
| 1 short | Milestone: cam OK / WiFi OK / upload OK / sleep |
| 1 long | Watchdog fired |
| 2 long | Camera init failed |
| 3 long | Camera capture failed |
| 4 long | PSRAM alloc failed |
| 5 long | WiFi — no AP found |
| 6 long | WiFi — auth rejected |
| 7 long | WiFi — timeout |
| 8 long | Upload failed |

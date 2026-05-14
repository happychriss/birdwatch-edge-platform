# CLAUDE.md

## Session Bootstrap

At the start of every session:
1. Read `/workspace/requirements.md` — project type, what is being built, current status. It links to the `skills/<type>-setup.md` that defines the working conventions for this project type.
2. Read all `.md` files in `/workspace/skills/` — active skills and conventions, including the setup file named in `requirements.md`
3. Read all `.md` files in `/workspace/knowledge/` — confirmed component and technology config

## File Ownership

- `CLAUDE.md` — for you: bootstrap and runtime context. This file.
- `requirements.md` — for you: what is being built. Start here for every task.
- `memory.md` — for you: live session memory. Keep it concise.
- `skills/` — for you: working conventions loaded each session.
- `knowledge/` — for you: confirmed config and integration notes.
- `external-docs/` — for you (read-only): raw reference material.
- `src/` — project source code.

## Runtime Environment

You are running inside a **Docker dev container** (Ubuntu, non-root user `ubuntu`):
- You have direct access to the filesystem and shell

## WiFi Architecture (never remove or change without understanding this)

The BirdWatch firmware uses **BSSID pinning** to the Fritz!Box primary router
(`b4:fc:7d:92:d4:90`) — set in `config.h` as `BW_WIFI_BSSID`. This is not cosmetic.
Root cause: Fritz!Box + mesh repeater share the same SSID; the repeater caused
`AUTH_FAIL` (reason=202) and `4WAY_HANDSHAKE_TIMEOUT` (reason=15) on most boots.
Pinning eliminates roaming to the repeater entirely.

Key constraints that must not be broken:
- **No `esp_wifi_restore()`** — would clear the NVS PMK cache (slower WPA2 handshake)
- **`esp_reset_reason() == ESP_RST_POWERON`** guard on `bw_power_reboot_safe()` — bounds
  the reboot chain to exactly one soft reboot per PIR event; SW/panic/WDT reboots fall
  through to power off
- **RTC GPIO domain in `bw_power_reboot_safe()`** — holds GPIO5 HIGH across `esp_restart()`
  so TPS22918 does not cut power during the ~500ms bootloader window
- **NVS BSSID cache cleared on miss** — stale cache is erased on failure so the next boot
  (after reboot) goes straight to scan rather than wasting another 10s

See `skills/wifi-esp32s3.md` for the full design rationale and timing budget.


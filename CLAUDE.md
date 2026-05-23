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

## Three-Project Consistency Rule

Any change to the cloud-detection algorithm must be kept in sync across all three projects.
**Before implementing, confirm with the user which projects need updating.**

Two filter layers, both synced:

### Burst-mode sequence filter (compares frame N to frame N-1)

| Parameter / behaviour | ESP firmware | Python reference | Validator |
|---|---|---|---|
| Burst thresholds | `cloud_check.c` `CC_BURST_*` `#define` | `BurstConfig` defaults in `burst_filter.py` | `validate.py` `_BURST_SUPPRESS_STAGES` |
| Stage logic | `run_pipeline()` burst block | `burst_classify()` in `burst_filter.py` | `validate.py` burst-aware comparison |
| NVS state | `cc_p` (tile means uint8) + `cc_pgm` (global mean uint8) | `prev_tile_mean`, `prev_gm` in-RAM | loaded from ESP `tile_means` telemetry |
| Telemetry fields | `burst_trigger`, `burst_label`, `burst_gm_diff`, `burst_n_changed`, `burst_n_dark` | `BurstResult` fields | `_get_py_field_burst()` in `validate.py` |
| dt-based stages (FAST_SHIFT, ISOLATED) | **omitted** — no wall clock before WiFi | present in `burst_filter.py` | skipped in validator (fall through to BG model) |

### Background-model pipeline (z-score vs long-term EMA model)

| Parameter / behaviour | ESP firmware | Python simulation | Validator config |
|---|---|---|---|
| Grid size | `cloud_check.c` `CC_TILES_X/Y` | `config.py` `grid_w/h`, `features.py` `GRID_W/H` | `validate_config.json` (implicit) |
| Thresholds (z, quiet ratio, warmup, deltas) | `cloud_check.c` `#define` | `config.py` defaults | `validate_config.json` `python_config` |
| Decision stages | `cloud_check.c` stage blocks | `classifier.py` branches | `validate_config.json` `checks` array |
| Telemetry field names | `bw_tele_*(name, …)` in `cloud_check.c` | `ClassifierResult` field names | `validate_config.json` `esp_key` / `py_field` |
| Field display names | `bw_tele_*` name | — | `display_spec.py` keys |

**Projects:**
- `src/esp_bw_src/` — ESP32-S3 firmware (C). Needs a flash to take effect.
- `src/cloud-check/` — Python algorithm package + parity validator. Used by the server.
- `src/python_bw_src/` — Flask web server + display spec. Runs on the local server.

## Flashing the ESP32

**You cannot flash remotely.** The device spends almost all its time in deep sleep (PIR-triggered), so `/dev/ttyACM0` is not available for flashing except during the brief active window.

Your role: **build and validate only.** The user flashes manually.

Workflow:
1. Make changes → `idf.py build` (from `/workspace/src/esp_bw_src/`, after `source ~/esp-idf/export.sh`)
2. Confirm clean build in the last lines of output
3. Commit + push
4. Tell the user the binary is ready — they flash with: `idf.py -p /dev/ttyACM0 flash`

**IDF version: v6** — lives inside the container at `/home/ubuntu/esp-idf/` (= `~/esp-idf/` as user `ubuntu`).

Claude Code runs on the host machine (`donald`, user `development`). The host has only IDF v5.5 at `/home/development/esp/esp-idf-v5.5/` and `/home/development/esp/esp-v5.5-dev/`. **Never source those paths and build** — doing so produces a v5.5 build directory at `project/src/esp_bw_src/build/` which, because the folder is bind-mounted as `/workspace/src/esp_bw_src/`, is immediately visible inside the container and breaks the v6 build until removed (`rm -rf project/src/esp_bw_src/build/`).

Always build inside the container shell (where `~/esp-idf/` is v6).

## Database Schema Rule

**Never propose adding columns to `bw_frames`.** The `meta` JSONB column stores all per-frame data — new fields go into `meta` as keys, no `ALTER TABLE` needed. The only promoted columns are `id`, `captured_at`, `result`, `filename`, `meta`. The `bw_photos` table (legacy) may still receive column migrations, but `bw_frames` must not.


# BirdWatch — Hardware

**Platform:** Seeed XIAO ESP32-S3 Sense (ESP32-S3, OV2640 camera module; newer boards may ship with OV3660 — see §4)

---

## 1. Power Architecture

The XIAO ESP32-S3 board is **not** left in deep sleep — it is fully power-gated off between events because board leakage in sleep is too high for the battery goal.

A **TPS22918** load switch (5.5 V, 2 A, 52 mΩ, SOT-23-6) sits between the LiPo and the XIAO BAT input. The **Parallax 555-28027 Rev B PIR sensor** stays always powered directly from the LiPo (it has up to 40 s warm-up/calibration time and must never be power-gated).

### 1.1 Wiring

```
LiPo+  ──── TPS22918 VIN
              TPS22918 VOUT ──── XIAO BAT
              TPS22918 ON   ◄─|─ PIR OUT  (D1: anode PIR, cathode ON)
                            ◄─|─ GPIO5/D4 (D2: anode GPIO5, cathode ON)
                            ──── 100 kΩ ──── GND   (mandatory pulldown)
              TPS22918 CT   ──── (not fitted — floating per datasheet)
              TPS22918 QOD  ──── (not fitted — floating)
LiPo─  ──── common GND (PIR, TPS22918, XIAO)

PIR VCC ──── LiPo+ (unswitched)
             10 µF + 100 nF to GND at PIR VCC pins

PIR OUT ──┬── 100 nF ──── GND  (RF filter — do not increase beyond 100 nF)
          ├── D1 ──── TPS22918 ON
          └── GPIO1 / D0  (ESP32 reads PIR state directly)
```

### 1.2 TPS22918 ON Node — Diode-OR Gate

The ON node is a diode-OR: either PIR or GPIO5 (or both) holds it HIGH independently. Both paths use Schottky diodes so neither driver loads the other.

The diode on the PIR path is essential: without it, PIR going LOW while GPIO5 is HIGH causes the PIR output transistor to fight GPIO5 on the ON node, potentially releasing TPS22918 mid-cycle.

| PIR OUT | GPIO5 | V_ON | TPS22918 |
|---------|-------|------|----------|
| HIGH | LOW | 3.4 V | ON — initial trigger before self-latch |
| HIGH | HIGH | 3.4 V | ON — normal running |
| LOW | HIGH | 3.0 V | ON — self-latch holds after PIR drops |
| LOW | LOW | 0.0 V | OFF — normal shutdown |

TPS22918 thresholds: V_IH_ON min = 1 V, V_IL_ON max = 0.5 V. All states are well within spec.

### 1.3 Self-Latch Sequence

| Step | Actor | Event |
|------|-------|-------|
| 1 | PIR | Detects movement → PIR HIGH → D1 forward-biased → TPS22918 ON HIGH → XIAO powers up |
| 2 | ESP32 | `bw_power_init()` — first call in `app_main()` — drives GPIO5 HIGH → D2 holds TPS22918 ON regardless of PIR |
| 3 | ESP32 | Normal operation: capture, filter, upload |
| 4 | ESP32 | `bw_power_release()` drives GPIO5 LOW → D2 reverse-biased; if PIR also LOW → 100 kΩ pulls ON to GND → TPS22918 off |
| 5 | ESP32 | `esp_deep_sleep_start()` as **fallback only** — only reached when USB/bench power keeps the board alive after TPS22918 release |

> **Critical:** GPIO5 must be driven HIGH in the very first lines of `app_main()`. A crash before `bw_power_init()` means the self-latch never asserts; once the PIR pulse drops, the TPS22918 cuts power and the event is lost.

### 1.4 Passives

| Component | Value | Purpose |
|-----------|-------|---------|
| ON node pulldown | 100 kΩ to GND | TPS22918 datasheet requirement — must not float |
| PIR VCC bulk | 10 µF (electrolytic, + to VCC) | Bulk reservoir |
| PIR VCC HF | 100 nF ceramic | HF decoupling, placed close to PIR |
| PIR OUT RF filter | 100 nF to GND | Attenuates 2.4 GHz WiFi coupling into PIR analog front-end |

### 1.5 Standby Current

Idle current is the always-powered PIR only: ~130 µA. XIAO is completely off between events.

---

## 2. PIR Sensor

**Parallax 555-28027 Rev B**

- Supply: 3–6 VDC; 130 µA idle, 3 mA active
- Output: active-high; source current 12 mA @ 3 V
- No adjustable hold time — output follows motion in real time
- Warm-up: up to 40 s after power-on; must remain always powered

### 2.1 WiFi RF Interference

WiFi TX bursts couple into the PIR's high-impedance analog gain stage (BISS0001 or similar), causing spurious triggers. Mitigations in order of effectiveness:

1. **Physical separation** — > 20 cm reduces field strength ~4×
2. **100 nF on PIR OUT to GND** — fitted; filters 2.4 GHz, keeps PIR response intact
3. **220 nF across BISS0001 pins 12 & 13** — directly filters the analog amplifier input
4. **Aluminum foil shield, grounded** — wrap PIR module body, connect foil to GND
5. **Ferrite bead on PIR VCC line**
6. **Lower WiFi TX power** — `esp_wifi_set_max_tx_power(40)` (10 dBm, reduces range 10×)

Note: firmware debounce does not help — the spurious PIR pulse from RF coupling is genuine (seconds long), not a glitch.

---

## 3. GPIO Pin Table

| GPIO | XIAO label | Function | Notes |
|------|-----------|----------|-------|
| 1 | D0 | PIR signal read / EXT1 wakeup | `INPUT_PULLDOWN`; reads PIR OUT directly (before diode to ON); EXT1 deep-sleep wakeup on fallback path |
| 2 | D1 | Battery ADC | `ADC1_CHANNEL_1`; voltage divider R1=100 kΩ, R2=220 kΩ → factor 320/220 = 1.4545× |
| 3 | D2 | RF antenna select | Output; HIGH = external U.FL, LOW = built-in; driven HIGH by `wifi_sta.c` during WiFi init |
| 4 | D3 | DS3231 SDA | Separate I2C bus from camera SCCB; `BW_DS3231_SDA_GPIO` |
| 5 | D4 | Power-hold / self-latch | Output; HIGH = hold TPS22918 ON, LOW = release → board loses power |
| 6 | D5 | DS3231 SCL | Separate I2C bus from camera SCCB; `BW_DS3231_SCL_GPIO` |
| 21 | LED_BUILTIN | Status LED | Active-low; blink patterns for lifecycle events |

---

## 4. Camera

- **Sensor:** OV2640 on boards manufactured before ~2024 (JPEG, up to 1600×1200); newer boards use OV3660 (2048×1536). Driver detects sensor at runtime via SCCB PID — OV2640 PID=0x26 confirmed in logs.
- **Photo mode:** JPEG, FRAMESIZE_SXGA, quality 10 (high quality)
- **AWB:** wb_mode=2 (Cloudy/6500K fixed matrix) — avoids green-cast failure seen with auto-AWB in mixed outdoor light
- **AE:** ae_level=+1 EV, aec_value=450 — lifts foreground exposure in high-contrast sky scenes; AGC on
- **Cloud-check mode:** Grayscale, FRAMESIZE_QQVGA (160×120) — captured before the main JPEG for the cloud-check filter; uses `CAMERA_GRAB_WHEN_EMPTY` to avoid frame-buffer overflow log noise
- **XCLK:** 16 MHz — 20 MHz causes continuous FB-OVF and NO-EOI on SXGA JPEG (OV2640 JPEG compressor cannot keep up at that PCLK for large frames)
- **Frame discard:** 6 frames at 100 ms intervals before main JPEG capture to let AEC/AGC converge after QQVGA→SXGA mode switch (~600 ms)
- **Exposure mode:** decided from cloud-check `global_mean` — `NORMAL` (≥ 130 DN) or `LOWLIGHT` (< 130 DN); transmitted as `photo_mode` field

### 4.1 Camera GPIO

| GPIO | Signal |
|------|--------|
| 10 | XCLK (16 MHz) |
| 40 | I2C SDA (SCCB) |
| 39 | I2C SCL (SCCB) |
| 15, 17, 18, 16, 14, 12, 11, 48 | Data bus D2–D9 |
| 38 | VSYNC |
| 47 | HREF |
| 13 | PCLK |

---

## 5. RTC Wakeup Circuit — Option A (Implemented)

DS3231 RTC provides timed wakeup independent of PIR. The circuit drives TPS22918 ON via a transistor stage.

### 5.1 Schematic

```
BAT+ ─── 10 kΩ pull-up ─── DS3231 INT/SQW
                                │
                               R1 (47 kΩ)
                                │
                              Q1 base  (2N3904 NPN)
                              Q1 emitter ─── GND
                              Q1 collector ─── ON_BUS

BAT+ ─── R3 (1 MΩ) ─── ON_BUS
ON_BUS ─── TPS22918 ON  (via same diode-OR node as PIR and GPIO5)
```

**No R2 — R2 was removed from the original design.**

### 5.2 Operating States

| DS3231 INT/SQW | Q1 | ON_BUS | TPS22918 |
|----------------|-----|--------|----------|
| HIGH (idle) | ON (saturated) | Vce_sat ≈ 0.1 V | OFF |
| LOW (alarm) | OFF | ~3.7 V (R3 pulls to BAT+) | ON |

- **Idle:** INT HIGH → base current through R1 (47 kΩ) → Q1 saturated → ON_BUS pulled to ~0.1 V → TPS22918 OFF.
- **Alarm:** DS3231 asserts INT LOW → Q1 base current ceases → Q1 off → R3 (1 MΩ) pulls ON_BUS to BAT+ (≈ 3.7 V) ≫ TPS22918 V_IH_ON (0.9 V) → TPS22918 ON → XIAO boots.

### 5.3 Idle Current

Idle current through R3: (3.7 V − 0.1 V) / 1 MΩ = **3.6 µA**

### 5.4 Risk Note

If the DS3231 fails and INT/SQW floats, Q1's base floats → Q1 may not turn on → ON_BUS is not pulled low → spurious XIAO boot possible on power application.

### 5.5 DS3231 I2C Pins

The DS3231 uses a **separate I2C bus** from the camera SCCB bus (different GPIOs to avoid address conflicts and bus contention):

| GPIO | XIAO label | Signal |
|------|-----------|--------|
| 4 | D3 | I2C SDA (`BW_DS3231_SDA_GPIO`) |
| 6 | D5 | I2C SCL (`BW_DS3231_SCL_GPIO`) |

The DS3231 INT/SQW pin uses a 10 kΩ pull-up to BAT+ (open-drain output). DS3231 VCC is on the always-on BAT+ rail (not switched by TPS22918) so the RTC keeps time and alarm state between power cycles.

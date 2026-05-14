# Power Architecture — TPS22918 Load Switch

## Overview

The XIAO ESP32-S3 board is fully power-gated between events. ESP32 deep sleep is not used as the primary idle state — dev-board leakage in sleep was too high for the battery goal. A **TPS22918** load switch (5.5 V, 2 A, 52 mΩ, SOT-23-6) connects LiPo power to the XIAO BAT input; the board is completely powered off between events.

## Wiring (as built)

```
LiPo+  ──── TPS22918 VIN
              TPS22918 VOUT ──── XIAO BAT
              TPS22918 ON   ◄─|─ PIR OUT  (D1: anode PIR, cathode ON)
                            ◄─|─ GPIO5/D4 (D2: anode GPIO5, cathode ON)
                            ──── 100 kΩ ──── GND   (mandatory pulldown)
              TPS22918 CT   ──── (not fitted — left floating, per datasheet OK)
              TPS22918 QOD  ──── (not fitted — left floating)
LiPo─  ──── common GND (PIR, TPS22918, XIAO)

PIR VCC ──── LiPo+ (unswitched)
PIR OUT ──┬── D1 (diode, anode PIR, cathode TPS22918 ON)
          └── GPIO1 / D0  (ESP32 reads PIR state directly)
         ├── 10 µF  ──── GND   (bulk decoupling, + to VCC if electrolytic)
         └── 100 nF ──── GND   (HF decoupling)
```

The TPS22918 ON node is a **diode-OR gate**: either PIR or GPIO5 (or both) can hold it HIGH, and neither driver sees the other's load. This is the correct topology when two independent sources must share an active-high enable.

## PIR Sensor

**Parallax 555-28027 Rev B** — active-high output, always powered directly from the unswitched LiPo side.

- Supply: 3–6 VDC; 130 µA idle, 3 mA active
- Output: HIGH when motion detected, LOW when not; source current 12 mA @ 3 V
- No adjustable hold time — output follows motion in real time
- Calibration: up to 40 s warm-up on its own power-up; must remain always powered
- Sensitivity jumper: S = reduced range (~15 ft), L = full range (~30 ft)

## GPIO1 (D0) — PIR state read

GPIO1 is connected directly to PIR OUT (before the diode to TPS22918 ON). It is configured `INPUT_PULLDOWN` in firmware and serves two purposes:

1. **Live PIR state read** — firmware can check whether the PIR is currently asserting motion
2. **EXT1 deep-sleep wakeup** — the fallback deep-sleep path (`bw_power_deep_sleep_pir_wake()`) configures EXT1 on GPIO1 so a PIR pulse wakes the chip if USB keeps the board alive after power release

The internal pulldown (~45 kΩ) draws ~82 µA from the PIR output when HIGH — well within the PIR's 12 mA source capability.

## Diode-OR operation — all four states

| PIR OUT | GPIO5 | V_ON  | TPS22918 |
|---------|-------|-------|----------|
| HIGH    | LOW   | 3.4 V | ON  — initial trigger before self-latch |
| HIGH    | HIGH  | 3.4 V | ON  — normal running |
| LOW     | HIGH  | 3.0 V | ON  — self-latch holds after PIR drops |
| LOW     | LOW   | 0.0 V | OFF — normal shutdown |

TPS22918 thresholds: V_IH_ON min = 1 V, V_IL_ON max = 0.5 V. All states are well within spec.

## Why diodes on both paths (not just GPIO5)

The original design connected PIR directly to TPS22918 ON. This had a hidden conflict:

When GPIO5 is HIGH (self-latch engaged) and PIR drops LOW, the PIR output transistor tries to pull the ON node to GND while GPIO5 pushes through its diode. The PIR can sink ~12 mA; the ON node would settle near PIR V_OL (~0.2–0.5 V) — right at or below TPS22918 V_IL_ON (0.5 V max). That could release TPS22918 and cut power mid-cycle.

With a diode on the PIR path: when PIR is LOW, D1 is reverse-biased. GPIO5 holds the ON node at 3.0 V through D2 with zero conflict. PIR going LOW can never disturb the self-latch.

## Fitted passive components

### ON node pulldown — 100 kΩ from TPS22918 ON to GND
Required by TPS22918 datasheet §8.3.1: *"This pin cannot be left floating and must be driven either high or low."*

During the ~300–600 ms boot window before `bw_power_init()` drives GPIO5 HIGH, neither driver is active. The pulldown ensures ON falls to GND if the PIR pulse drops during this window — the board loses power cleanly rather than the ON node floating.

Both diodes drive into 100 kΩ; their source currents (12 mA PIR, 40 mA GPIO5) dwarf the 37–74 µA pulldown load.

### PIR VCC decoupling — 10 µF + 100 nF, at PIR power pins
- 100 nF ceramic (104), 10 V or 16 V, HF filtering
- 10 µF in parallel, bulk reservoir; if electrolytic: + to VCC, – to GND
- Placed physically close to PIR VCC/GND pins

## CT capacitor — not fitted

The CT cap controls only the TPS22918 VOUT slew rate at turn-on. The main current surge in this system occurs when **WiFi is initialised** — several seconds into the boot cycle, well after `bw_power_init()` has driven GPIO5 HIGH. The CT cap has no effect on that surge. Real-world operation confirmed the turn-on inrush does not cause observable problems, so CT is left floating (TPS22918 pin table: "can be left floating").

## Self-latch sequence

1. PIR detects motion → PIR HIGH → D1 forward-biased → ON node HIGH → TPS22918 ON → XIAO powers up
2. `bw_power_init()` (first call in `app_main()`) → GPIO5 HIGH → D2 forward-biased → ON node held HIGH regardless of PIR
3. Normal operation: capture, upload, etc.
4. `bw_power_release()` → GPIO5 LOW → D2 reverse-biased
   - If PIR still HIGH: D1 holds ON HIGH → TPS22918 stays on → firmware enters deep sleep fallback, wakes when PIR drops
   - If PIR also LOW: both diodes off → 100 kΩ pulls ON to GND → TPS22918 cuts power

## Software contract

| Event | Firmware action |
|---|---|
| Power-on (PIR fires TPS22918) | `bw_power_init()` — first call; configures GPIO5 as output, drives HIGH immediately |
| Normal shutdown | flush → deinit → `bw_power_release()` drops GPIO5 LOW → TPS22918 cuts power |
| USB / bench fallback | `bw_power_deep_sleep_pir_wake()` — EXT1 on GPIO1, enters deep sleep |

## Cold boot invariant

Every PIR event is a full cold boot — no RTC memory or in-RAM state survives between events.

## Standby floor

Idle current is the always-powered PIR only (130 µA idle). XIAO is completely off between events.

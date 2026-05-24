# Project Requirements

## Project Type

Follow `/workspace/skills/project-setup.md` for working conventions, folder structure, knowledge flow, and development workflow.

---

## Project Summary

BirdWatch is a battery-powered, outdoor wildlife camera built on the Seeed XIAO ESP32-S3 Sense. It wakes on PIR motion, applies a two-stage cloud/false-trigger filter entirely on-device, and uploads bird photos to a home Flask server over WiFi. The board is fully power-gated between events (TPS22918 load switch) rather than using deep sleep, because board leakage in sleep is too high for the battery goal. Every boot is a cold boot; no state survives in RAM between events. The cloud-detection algorithm runs in C on-device and is mirrored in Python for server-side validation and offline calibration.

---

## Detail Files

- **[requirements_main.md](requirements_main.md)** — cycle lifecycle, state machine, watchdog, blink codes, NVS usage, server endpoints, WiFi architecture, credentials, build/flash workflow, training data, known issues.
- **[requirements_model.md](requirements_model.md)** — cloud-detection algorithm: burst-mode sequence filter, background-model pipeline, stage logic, thresholds, z-score, tile grid, telemetry fields, full pipeline reference table, calibration and validator notes.
- **[requirements_hardware.md](requirements_hardware.md)** — all hardware wiring: TPS22918 power architecture, PIR sensor, GPIO pin table, camera GPIOs, RF antenna select, battery ADC, RTC wakeup circuit (Option A, implemented).

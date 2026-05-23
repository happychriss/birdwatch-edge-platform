# cloud-check — PIR false-trigger filter

Python simulation of the on-device cloud-vs-non-cloud classifier.  
Full spec: [`requirements-cloud-detection.md`](../../requirements-cloud-detection.md)

---

## Burst-mode sequence filter (`burst_filter.py`)

Runs **before** the background-model pipeline. Compares each frame to the *previous captured frame* to suppress PIR false triggers caused by cloud/sun dynamics around noon.

**Core physics:** Birds create DARK regions against a bright sky. Scene brightening (dark→bright) is a sun event, not a bird. Only newly-dark tiles carry object information.

### Decision stages (in order)

| Stage | Condition | Decision | Physical reason |
|-------|-----------|----------|-----------------|
| FIRST | no previous frame | process | no baseline |
| ISOLATED | dt > 180 s | process | independent event, outside burst window |
| FAST_SHIFT | \|gm_diff\| > 12 AND dt < 15 s AND gm ≥ 120 | suppress | PIR re-fired on same cloud transition; no time for bird to coincide |
| BRIGHTNESS_SHIFT | \|gm_diff\| > 12 | process | whole-scene illumination change; bird could have appeared |
| DUPLICATE | n_changed ≤ 0 | suppress | pixel-identical; PIR triplet on same IR stimulus |
| BRIGHT_STABLE | gm > 160 AND n_dark < 35 | suppress | bright scene + few dark tiles = no shadow-casting object |
| DIFFUSE | n_dark ≥ 60 | suppress | cloud shadow sweeping entire scene |
| SAFE | default | process | safety bias — upload rather than miss a bird |

### Parameters and physical meaning

| Parameter | Value | Physical meaning | Safety margin |
|-----------|-------|-----------------|---------------|
| `burst_window_seconds` | 180 s | max gap still considered "in burst" | conservative; PIR clusters rarely span >3 min |
| `brightness_sim_threshold` | 12 DN | \|gm_curr − gm_prev\| threshold for whole-scene shift | 2.3 DN below hardest bird gm_diff (14.3) |
| `tile_diff_threshold` | 12 DN | tile must change >12 DN to count as n_changed (abs diff) | matched to dark_diff_threshold |
| `dark_diff_threshold` | 12 DN | tile must get >12 DN *darker* to count as n_dark | at 12 DN, hardest bird n_dark drops below diffuse_min=60 |
| `duplicate_max_tiles` | 0 | n_changed ≤ 0 → DUPLICATE; 1–2 tiles → always process | enforces user rule: 1–2 tile changes always processed |
| `diffuse_min_dark_tiles` | 60 | ≥ 60 dark tiles → cloud shadow | all bird/pillow n_dark confirmed < 60 at td=12 |
| `fast_shift_max_dt` | 15 s | FAST_SHIFT only if dt < 15 s | 12 s below nearest bird BRIGHTNESS_SHIFT (dt=27 s) |
| `fast_shift_min_gm` | 120 DN | FAST_SHIFT only in bright scenes | excludes dawn/shade where fast shift has other causes |
| `bright_stable_min_gm` | 160 DN | BRIGHT_STABLE requires scene ≥ 160 | 9 DN above highest known in-burst real-bird brightness (151.2) |
| `bright_stable_max_dark` | 35 | BRIGHT_STABLE only if n_dark < 35 | headroom above nd=0 (no sunny-bird data yet — revisit) |

### Results (training data, 2026-05-22, 314 total frames)

| Folder | Frames | Suppressed | Errors |
|--------|--------|------------|--------|
| ignore-sun_shining | 224 | **103 (46%)** | 0 |
| process-birds-pillow | 26 | 0 | 0 ✓ |
| process-real-birds | 18 | 0 | 0 ✓ |
| process-people | 46 | 5 (11%) | 5 (acceptable) |

Suppression breakdown: FAST_SHIFT=40, DUPLICATE=36, BRIGHT_STABLE=17, DIFFUSE=10.

### Run burst evaluation

```bash
cd src/cloud-check
python validate_burst.py               # summary
python validate_burst.py --detail      # per-frame output
python validate_burst.py --sweep       # grid search over key thresholds
python generate_burst_gallery.py       # HTML gallery → reports/burst_gallery.html
```

### Open risks

- `bright_stable_max_dark=35` calibrated with no in-burst real-bird data above gm=160. Revisit as sunny-bird data is collected.
- `fast_shift_max_dt=15 s` has a 12 s margin on a small dataset. Watch as real-bird data grows.
- Sun passthrough 54% — remaining frames handled downstream by background-model pipeline.

---

## Background-model pipeline results (147 real frames, online self-calibrating mode)

| | Non-cloud recall | Cloud recall |
|---|---|---|
| Online (self-calibrating) | **1.000** — 0 birds missed | **0.606** |
| Oracle (ground-truth updates) | 1.000 | 0.688 |

## Decision pipeline

| Stage | Fires when | Decision | Updates model |
|-------|-----------|----------|---------------|
| NIGHT | global mean < 70 | upload | yes |
| WARMUP | < 8 frames seen | upload | yes (bootstrap) |
| DARK_OBJ | tiles newly darker than model AND previous frame | upload | no |
| INDIRECT_LIGHT | global mean 70–95 (low-angle sun) | upload | yes |
| SPOT_CHANGE | 1–2 tiles darkened vs prev, scene globally stable | upload | no |
| QUIET | ≤ 20 % tiles anomalous | suppress | yes |
| SCENE_DRIFT | tiles dark vs model but NOT vs previous frame | upload | yes (re-calibrate) |
| AMBIGUOUS | default | upload | no |

## Package layout

```
cloud_check/
  config.py       all tunable parameters (one frozen dataclass)
  dataset.py      training-data loader + time_bucket()
  features.py     VGA → 16×12 tile mean extraction
  background.py   per-tile EMA model, 4 day-period buckets
  classifier.py   decision pipeline (z-score, blob, temporal diff)
  pipeline.py     online evaluation harness + CSV writer

scripts/
  cloud-check     shell: run evaluate + start server (kill/restart safe)
  evaluate.py     confusion matrix over labelled set → reports/
  sweep.py        exhaustive parameter grid search + stage ablation
  inspect.py      debug one frame: replay history, render anomaly mask PNG
  show_gallery.py generate static reports/gallery.html
  synthesize.py   augment training data (lighting shifts, bird paste)

serve.py          Flask server: POST /assess, GET /gallery, GET /model/status
```

## Quick start

```bash
cd src/cloud-check
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Evaluate + start gallery server (kills any running instance first)
../scripts/cloud-check.sh
# → http://localhost:8001/gallery

# Evaluate only
.venv/bin/python -m scripts.evaluate --skip-aux

# Parameter sweep (18k configs, ~30s on cached features)
.venv/bin/python -m scripts.sweep

# Inspect one frame
.venv/bin/python -m scripts.inspect 20260515_110118.jpg
```

## Parity validator

`validate.py` replays captured frames from the home server database against the Python classifier and diffs every configured value against what the ESP reported in its telemetry.

**Configure checks** in `validate_config.json` — one line per value, no code change:
```json
{
  "time_frame": { "from": "-24h", "to": "now" },
  "checks": [
    { "esp_key": "result",     "py_field": "label",        "type": "exact" },
    { "esp_key": "stage",      "py_field": "trigger",       "type": "exact" },
    { "esp_key": "dark_tiles", "py_field": "dark_tiles",    "type": "int"   },
    { "esp_key": "ratio",      "py_field": "anomaly_ratio", "type": "float", "tol": 0.001 }
  ]
}
```

`type` is `exact`, `int`, `float`, or `bool`. `float` accepts an optional `tol` tolerance.  
`time_frame` accepts ISO timestamps, `"now"`, `"-24h"`, `"-7d"`.

**Input source:** the validator queries `bw_frames` in the home server DB and pulls each JPEG over HTTP — no filesystem mount needed.

**Flash anchor:** if a `fresh_flash` marker is present in any frame's meta, the Python `BackgroundModel` is reset at that point. This ensures the Python and ESP models start from identical priors after every firmware flash, making parity meaningful from frame 1.

**Run from command line:**
```bash
cd src/cloud-check
.venv/bin/python validate.py                        # human-readable output
.venv/bin/python validate.py validate_config.json --json   # JSON for Flask
```

**Run from web UI:** `GET /validate` → Run validation button.  
Results show every checked value per frame: **green** = match, **red** = mismatch.

## Why not ML?

The whole pipeline is integer-arithmetic-friendly signal processing — no neural network, no training, no TFLite. Every suppressed frame shows exactly which rule fired and why. The background model self-calibrates from live captures; no re-training when the scene changes.

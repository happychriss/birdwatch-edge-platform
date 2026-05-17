# cloud-check — PIR false-trigger filter

Python simulation of the on-device cloud-vs-non-cloud classifier.  
Full spec: [`requirements-cloud-detection.md`](../../requirements-cloud-detection.md)

## Results (147 real frames, online self-calibrating mode)

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

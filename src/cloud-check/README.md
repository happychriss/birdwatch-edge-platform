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
| WARMUP | < 8 frames seen in bucket | upload | yes (bootstrap) |
| DARK_OBJ | tiles newly darker than model AND previous frame | upload | no |
| QUIET | ≤ 5 % tiles anomalous | suppress | yes |
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

## Why not ML?

The whole pipeline is integer-arithmetic-friendly signal processing — no neural network, no training, no TFLite. Every suppressed frame shows exactly which rule fired and why. The background model self-calibrates from live captures; no re-training when the scene changes.

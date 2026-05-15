"""BirdWatch cloud-check assessment server.

Maintains a persistent background model in RAM. Accepts JPEG images via HTTP,
runs the classifier, and returns the result as JSON.  Also serves a gallery of
all training-data photos with their live assessments.

Usage:
    cd /workspace/src/cloud-check
    .venv/bin/python serve.py [--port 8001] [--oracle]

Endpoints:
    POST /assess            Classify one image. Returns JSON assessment.
    GET  /gallery           HTML gallery of all training data + assessments.
    GET  /image/<rel>       Serve a training-data image (for gallery thumbnails).
    GET  /model/status      Background model bucket statistics.
    POST /model/reset       Wipe the background model back to defaults.

ESP integration (future):
    The firmware will POST /assess before deciding whether to upload.
    If label=="cloud", skip upload. If "non-cloud", upload to main server.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cloud_check.background import BackgroundModel
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.dataset import DATASET_ROOT, Sample, load_dataset
from cloud_check.features import (
    GRID_H,
    GRID_W,
    TILE_H,
    TILE_W,
    extract_tile_features,
    load_gray_vga,
)

# ---------------------------------------------------------------------------
# Singleton model (lives for the lifetime of the server process)
# ---------------------------------------------------------------------------

_cfg = Config()
_model = BackgroundModel(_cfg)
_model_lock = threading.Lock()
_assess_count = 0
_start_time = time.time()
_prev_tile_mean: dict[int, "np.ndarray"] = {}  # bucket_idx → last tile_mean


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/assess", methods=["POST"])
def assess():
    """Classify one image.

    Accepts:
        Body: raw JPEG bytes   (Content-Type: image/jpeg)
          OR multipart field "image" with JPEG file

    Query params:
        hour  int  hour-of-day override (default: current wall-clock hour)

    Returns JSON:
        {
          "label":         "cloud" | "non-cloud",
          "trigger":       "WARMUP" | "DARK_OBJ" | "QUIET" | "SCENE_DRIFT" | "AMBIGUOUS",
          "reason":        "...",
          "blob_max":      int,
          "anomaly_ratio": float,
          "compactness":   float,
          "warmup":        bool,
          "action":        "suppress" | "upload",
          "assessed_at":   ISO timestamp
        }
    """
    global _assess_count

    # Accept raw body or multipart
    if request.content_type and request.content_type.startswith("multipart"):
        f = request.files.get("image")
        if f is None:
            return jsonify(error="no 'image' field in multipart"), 400
        img_bytes = f.read()
    else:
        img_bytes = request.get_data()
    if not img_bytes:
        return jsonify(error="empty body"), 400

    hour_param = request.args.get("hour")
    hour = int(hour_param) if hour_param is not None else datetime.now().hour

    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(img_bytes)).convert("L").resize((640, 480))
        import numpy as np
        frame = np.asarray(im, dtype=np.uint8)
    except Exception as e:
        return jsonify(error=f"could not decode image: {e}"), 400

    feats = extract_tile_features(frame)

    with _model_lock:
        bucket = _model._idx(hour)
        prev = _prev_tile_mean.get(bucket)
        was_warmup = _model.warmup_remaining(hour) > 0
        _model.observe(hour)
        result = classify(feats["mean"], hour, _model, _cfg, prev_tile_mean=prev)
        if was_warmup or result.label == "cloud" or result.trigger == "SCENE_DRIFT":
            _model.update(hour, feats["mean"])
        _prev_tile_mean[bucket] = feats["mean"]
        _assess_count += 1

    return jsonify(
        label=result.label,
        trigger=result.trigger,
        reason=result.reason,
        blob_max=result.blob_max_size,
        anomaly_ratio=round(result.anomaly_ratio, 4),
        compactness=round(result.compactness, 4),
        warmup=result.warmup,
        action="suppress" if result.label == "cloud" else "upload",
        assessed_at=datetime.now().isoformat(timespec="seconds"),
    )


@app.route("/image/<path:rel>")
def serve_image(rel: str):
    """Serve a training-data image by its relative path under DATASET_ROOT."""
    target = (DATASET_ROOT / rel).resolve()
    if not str(target).startswith(str(DATASET_ROOT)):
        return "forbidden", 403
    if not target.is_file():
        return "not found", 404
    return send_file(target, mimetype="image/jpeg")


@app.route("/model/status")
def model_status():
    with _model_lock:
        bucket_info = []
        for b in range(_cfg.num_time_buckets):
            start_h = _cfg.day_start_hour + b * ((_cfg.day_end_hour - _cfg.day_start_hour) // _cfg.num_time_buckets)
            end_h = start_h + ((_cfg.day_end_hour - _cfg.day_start_hour) // _cfg.num_time_buckets)
            bucket_info.append({
                "bucket": b,
                "hours": f"{start_h:02d}–{end_h:02d}",
                "observations": int(_model.bucket_seen[b]),
                "warmup_remaining": _model.warmup_remaining(start_h),
                "tile_mean_avg": round(float(_model.mean[b].mean()), 1),
                "tile_var_avg": round(float(_model.var[b].mean()), 1),
            })
    return jsonify(
        uptime_s=round(time.time() - _start_time, 1),
        assess_count=_assess_count,
        config={k: v for k, v in _cfg.__dict__.items()},
        buckets=bucket_info,
    )


@app.route("/model/reset", methods=["POST"])
def model_reset():
    global _model, _assess_count, _prev_tile_mean
    with _model_lock:
        _model = BackgroundModel(_cfg)
        _assess_count = 0
        _prev_tile_mean = {}
    return jsonify(status="reset", timestamp=datetime.now().isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

_TRIGGER_COLOR = {
    "WARMUP":       "#9b59b6",
    "DARK_OBJ":     "#2ecc71",
    "QUIET":        "#3498db",
    "SCENE_DRIFT":  "#f39c12",
    "AMBIGUOUS":    "#e67e22",
}

_GALLERY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BirdWatch cloud-check gallery</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #1a1a2e; color: #eee; font-family: monospace; font-size: 13px; }
  header { padding: 14px 20px; background: #16213e; display: flex; align-items: center; gap: 20px; }
  header h1 { font-size: 16px; font-weight: bold; color: #e2e2e2; }
  .stats { font-size: 12px; color: #aaa; }
  .legend { display: flex; gap: 10px; margin-left: auto; flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: 4px; font-size: 11px; }
  .swatch { width: 12px; height: 12px; border-radius: 2px; }
  .grid { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px; }
  .card {
    width: 220px; border-radius: 6px; overflow: hidden;
    border: 3px solid transparent; background: #0f3460; position: relative;
  }
  .card.correct-cloud   { border-color: #3498db; }
  .card.correct-ncloud  { border-color: #2ecc71; }
  .card.fp              { border-color: #e74c3c; border-width: 4px; }
  .card.fn              {
    border-color: #ff0000; border-width: 6px;
    box-shadow: 0 0 18px 6px #ff000099;
    animation: fn-pulse 1.2s ease-in-out infinite alternate;
  }
  @keyframes fn-pulse {
    from { box-shadow: 0 0 12px 4px #ff000066; }
    to   { box-shadow: 0 0 28px 10px #ff0000cc; }
  }
  .card img { width: 100%; display: block; }
  .info { padding: 6px 8px; font-size: 11px; line-height: 1.6; }
  .filename { font-size: 10px; color: #888; word-break: break-all; }
  .label-row { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
  .badge {
    padding: 1px 6px; border-radius: 10px; font-size: 10px; font-weight: bold;
    letter-spacing: .5px; text-transform: uppercase;
  }
  .badge-cloud    { background: #2980b9; color: #fff; }
  .badge-ncloud   { background: #27ae60; color: #fff; }
  .badge-wrong    { background: #c0392b; color: #fff; }
  .trigger-badge  { padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: bold; }
  .detail { color: #aaa; font-size: 10px; }
  .wrong-marker {
    position: absolute; top: 4px; right: 4px;
    color: #fff; font-size: 10px; font-weight: bold;
    padding: 2px 5px; border-radius: 3px;
  }
  .wrong-marker.fp-marker { background: #c0392b; }
  .wrong-marker.fn-marker { background: #ff0000; font-size: 12px; letter-spacing: 1px; }
</style>
</head>
<body>
<header>
  <h1>BirdWatch — cloud-check gallery</h1>
  <span class="stats">{{ n_total }} frames · TP={{ tp }} FN={{ fn }} FP={{ fp }} TN={{ tn }}
    · non-cloud recall={{ "%.3f"|format(nc_recall) }}
    · cloud recall={{ "%.3f"|format(c_recall) }}</span>
  <div class="legend">
    {% for trigger, color in trigger_colors.items() %}
    <div class="legend-item">
      <div class="swatch" style="background:{{ color }}"></div><span>{{ trigger }}</span>
    </div>
    {% endfor %}
    <div class="legend-item"><div class="swatch" style="background:#e74c3c"></div><span>FP (cloud uploaded)</span></div>
    <div class="legend-item"><div class="swatch" style="background:#ff0000; box-shadow:0 0 6px #ff0000"></div><span>FN ⚠ BIRD MISSED</span></div>
  </div>
</header>
<div class="grid">
{% for r in results %}
  {% set correct = r.pred.label == r.sample.label %}
  {% set is_fn = (not correct) and r.sample.label == "non-cloud" %}
  {% set is_fp = (not correct) and r.sample.label == "cloud" %}
  {% if correct and r.sample.label == "cloud" %}{% set card_cls = "correct-cloud" %}
  {% elif correct %}{% set card_cls = "correct-ncloud" %}
  {% elif is_fn %}{% set card_cls = "fn" %}
  {% else %}{% set card_cls = "fp" %}{% endif %}
  <div class="card {{ card_cls }}">
    {% if is_fn %}<div class="wrong-marker fn-marker">⚠ BIRD MISSED</div>
    {% elif is_fp %}<div class="wrong-marker fp-marker">✗ FP</div>{% endif %}
    <img src="/image/{{ r.sample.path.relative_to(dataset_root) }}"
         loading="lazy"
         title="{{ r.pred.reason }}">
    <div class="info">
      <div class="filename">{{ r.sample.path.name }}</div>
      <div class="label-row">
        <span class="badge {{ 'badge-ncloud' if r.sample.label == 'non-cloud' else 'badge-cloud' }}">
          truth: {{ r.sample.label }}
        </span>
        <span class="badge {{ 'badge-ncloud' if r.pred.label == 'non-cloud' else ('badge-cloud' if correct else 'badge-wrong') }}">
          pred: {{ r.pred.label }}
        </span>
      </div>
      <div class="label-row">
        <span class="trigger-badge" style="background:{{ trigger_colors[r.pred.trigger] }}; color:#fff">
          {{ r.pred.trigger }}
        </span>
        <span class="detail">hour {{ r.sample.hour_bucket }}</span>
      </div>
      <div class="detail">blob={{ r.pred.blob_max_size }}
        ratio={{ "%.2f"|format(r.pred.anomaly_ratio) }}
        cmp={{ "%.2f"|format(r.pred.compactness) }}
        nd={{ r.pred.new_dark_tiles }}</div>
    </div>
  </div>
{% endfor %}
</div>
</body>
</html>"""


@app.route("/gallery")
def gallery():
    """Replay all training data chronologically through a fresh model and render HTML."""
    samples = load_dataset()
    samples = [s for s in samples if s.domain != "aux-2025"]
    samples = sorted(samples, key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name))

    gal_cfg = Config()
    gal_model = BackgroundModel(gal_cfg)
    gal_prev: dict[int, "np.ndarray"] = {}

    class R:
        pass

    results = []
    for s in samples:
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame)
        bucket = gal_model._idx(s.hour_bucket)
        prev = gal_prev.get(bucket)
        was_warmup = gal_model.warmup_remaining(s.hour_bucket) > 0
        gal_model.observe(s.hour_bucket)
        pred = classify(feats["mean"], s.hour_bucket, gal_model, gal_cfg, prev_tile_mean=prev)
        if was_warmup or pred.label == "cloud" or pred.trigger == "SCENE_DRIFT":
            gal_model.update(s.hour_bucket, feats["mean"])
        gal_prev[bucket] = feats["mean"]
        r = R()
        r.sample = s
        r.pred = pred
        results.append(r)

    tp = sum(1 for r in results if r.sample.label == "non-cloud" and r.pred.label == "non-cloud")
    fn = sum(1 for r in results if r.sample.label == "non-cloud" and r.pred.label == "cloud")
    fp = sum(1 for r in results if r.sample.label == "cloud"     and r.pred.label == "non-cloud")
    tn = sum(1 for r in results if r.sample.label == "cloud"     and r.pred.label == "cloud")
    nc_recall = tp / (tp + fn) if (tp + fn) else 0.0
    c_recall  = tn / (tn + fp) if (tn + fp) else 0.0

    html = render_template_string(
        _GALLERY_TEMPLATE,
        results=results,
        dataset_root=DATASET_ROOT,
        trigger_colors=_TRIGGER_COLOR,
        n_total=len(results),
        tp=tp, fn=fn, fp=fp, tn=tn,
        nc_recall=nc_recall,
        c_recall=c_recall,
    )
    return Response(html, mimetype="text/html")


@app.route("/")
def index():
    return jsonify(
        service="birdwatch-cloud-check",
        endpoints={
            "POST /assess":       "classify an image (raw JPEG body or multipart 'image' field)",
            "GET  /gallery":      "HTML gallery of all training data",
            "GET  /image/<rel>":  "serve a training-data image",
            "GET  /model/status": "background model statistics",
            "POST /model/reset":  "wipe the background model",
        },
        assess_count=_assess_count,
        uptime_s=round(time.time() - _start_time, 1),
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    print(f"cloud-check server → http://{args.host}:{args.port}/")
    print(f"  gallery  → http://localhost:{args.port}/gallery")
    print(f"  status   → http://localhost:{args.port}/model/status")
    app.run(host=args.host, port=args.port, debug=False)

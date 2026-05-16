"""Generate a static HTML gallery: all training photos with classifier output.

Usage (from /workspace/src/cloud-check):
    .venv/bin/python -m scripts.show_gallery
    # opens reports/gallery.html — serve with the Flask server to view images.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.background import BackgroundModel
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.features import extract_tile_features, load_gray_vga

REPORT = Path(__file__).resolve().parents[1] / "reports"

TRIGGER_COLOR = {
    "NIGHT":          "#1a1a2e",
    "WARMUP":         "#9b59b6",
    "DARK_OBJ":       "#2ecc71",
    "INDIRECT_LIGHT": "#e74c3c",
    "SPOT_CHANGE":    "#ff6b35",
    "QUIET":          "#3498db",
    "SCENE_DRIFT":    "#f39c12",
    "AMBIGUOUS":      "#e67e22",
}

HTML_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>BirdWatch gallery</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #1a1a2e; color: #eee; font-family: monospace; font-size: 13px; }
header { padding: 14px 20px; background: #16213e; }
h1 { font-size: 16px; margin-bottom: 6px; }
.stats { font-size: 12px; color: #aaa; }
.legend { display: flex; gap: 12px; margin-top: 8px; flex-wrap: wrap; }
.swatch { display: inline-block; width: 11px; height: 11px; border-radius: 2px; vertical-align: middle; margin-right: 3px; }
.grid { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px; }
.card { width: 220px; border-radius: 6px; overflow: hidden; background: #0f3460;
        border: 3px solid #555; position: relative; }
.card.ok-cloud  { border-color: #2980b9; }
.card.ok-obj    { border-color: #27ae60; }
.card.wrong     { border-color: #e74c3c; border-width: 4px; }
.card img       { width: 100%; display: block; }
.wrong-marker   { position: absolute; top: 4px; right: 4px; background: #e74c3c;
                  color: #fff; font-size: 10px; font-weight: bold; padding: 2px 5px; border-radius: 3px; }
.info { padding: 5px 7px; font-size: 11px; line-height: 1.7; }
.fname { font-size: 10px; color: #777; word-break: break-all; }
.row { display: flex; gap: 5px; align-items: center; flex-wrap: wrap; }
.badge { padding: 1px 6px; border-radius: 10px; font-size: 10px; font-weight: bold; }
.b-obj    { background: #27ae60; color:#fff; }
.b-cloud  { background: #2980b9; color:#fff; }
.b-wrong  { background: #c0392b; color:#fff; }
.b-trigger { padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: bold; color:#fff; }
.dim { color: #888; font-size: 10px; }
</style></head><body>
"""


def run() -> None:
    samples = [s for s in load_dataset() if s.domain != "aux-2025"]
    samples.sort(key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name))

    cfg = Config()
    model = BackgroundModel(cfg)
    prev_tile_mean: dict = {}

    rows = []
    for s in samples:
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame)
        bucket = model._idx(s.hour_bucket)
        prev = prev_tile_mean.get(bucket)
        was_warmup = model.warmup_remaining(s.hour_bucket) > 0
        model.observe(s.hour_bucket)
        pred = classify(feats["mean"], s.hour_bucket, model, cfg, prev_tile_mean=prev)
        if was_warmup or pred.label == "clouds" or pred.trigger in (
            "SCENE_DRIFT", "NIGHT", "INDIRECT_LIGHT"
        ):
            model.update(s.hour_bucket, feats["mean"])
        if pred.trigger == "SCENE_DRIFT":
            model.reset_warmup(s.hour_bucket)
        prev_tile_mean[bucket] = feats["mean"]
        rows.append((s, pred))

    tp = sum(1 for s, p in rows if s.label == "process" and p.label == "process")
    fn = sum(1 for s, p in rows if s.label == "process" and p.label == "clouds")
    fp = sum(1 for s, p in rows if s.label == "clouds"  and p.label == "process")
    tn = sum(1 for s, p in rows if s.label == "clouds"  and p.label == "clouds")
    proc_recall  = tp / (tp + fn) if (tp + fn) else 0
    cloud_recall = tn / (tn + fp) if (tn + fp) else 0

    legend = " ".join(
        f'<span><span class="swatch" style="background:{c}"></span>{t}</span>'
        for t, c in TRIGGER_COLOR.items()
    ) + ' <span><span class="swatch" style="border:2px solid #e74c3c"></span>WRONG</span>'

    def thumb_b64(path: Path, size: int = 220) -> str:
        """Return a base64-encoded JPEG thumbnail as a data URI."""
        img = Image.open(path).convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    cards = []
    for s, pred in rows:
        correct = pred.label == s.label
        if correct and s.label == "clouds":  cls = "ok-cloud"
        elif correct:                         cls = "ok-obj"
        else:                                 cls = "wrong"

        truth_cls  = "b-obj"   if s.label    == "process" else "b-cloud"
        pred_cls   = "b-obj"   if pred.label == "process" else ("b-cloud" if correct else "b-wrong")
        trig_color = TRIGGER_COLOR.get(pred.trigger, "#888")
        src        = thumb_b64(s.path)
        wrong_marker = '<div class="wrong-marker">✗ MISSED</div>' if not correct else ""
        reason_esc = pred.reason.replace('"', '&quot;')
        cards.append(f"""
<div class="card {cls}" title="{reason_esc}">
  {wrong_marker}
  <img src="{src}" loading="lazy">
  <div class="info">
    <div class="fname">{s.path.name}</div>
    <div class="row">
      <span class="badge {truth_cls}">truth:{s.label}</span>
      <span class="badge {pred_cls}">pred:{pred.label}</span>
    </div>
    <div class="row">
      <span class="b-trigger" style="background:{trig_color}">{pred.trigger}</span>
      <span class="dim">h{s.hour_bucket} r={pred.anomaly_ratio:.2f} nd={pred.new_dark_tiles}</span>
    </div>
  </div>
</div>""")

    REPORT.mkdir(exist_ok=True)
    out = REPORT / "gallery.html"
    with out.open("w") as f:
        f.write(HTML_HEAD)
        f.write(f"""<header><h1>BirdWatch — cloud-check gallery</h1>
<div class="stats">{len(rows)} frames &nbsp;·&nbsp;
  TP={tp} FN={fn} FP={fp} TN={tn} &nbsp;·&nbsp;
  process recall={proc_recall:.3f} &nbsp;·&nbsp;
  clouds recall={cloud_recall:.3f} &nbsp;·&nbsp; thumbnails embedded (standalone HTML)</div>
<div class="legend">{legend}</div></header>\n""")
        f.write('<div class="grid">\n')
        f.writelines(cards)
        f.write("\n</div></body></html>")

    print(f"wrote {out}  ({len(rows)} cards, self-contained)")


if __name__ == "__main__":
    run()

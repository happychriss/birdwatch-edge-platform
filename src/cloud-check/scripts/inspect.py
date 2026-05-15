"""Visualise a single frame: render the VGA grayscale input with the tile
anomaly mask overlaid. Run AFTER evaluate.py so the bg model is reproduced
by replaying the same chronological stream up to (but not including) the
target frame.

Usage (from /workspace/cloud-check):
    .venv/bin/python -m scripts.inspect 20260514_181000.jpg
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.features import (
    FRAME_H,
    FRAME_W,
    GRID_H,
    GRID_W,
    TILE_H,
    TILE_W,
    extract_tile_features,
    load_gray_vga,
)
from cloud_check.background import BackgroundModel


REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: inspect.py <filename.jpg> [--oracle]")
        sys.exit(1)
    target_name = sys.argv[1]
    oracle = "--oracle" in sys.argv[2:]
    include_aux = "--include-aux" in sys.argv[2:]

    cfg = Config()
    samples = load_dataset()
    if not include_aux:
        samples = [s for s in samples if s.domain == "real-2026"]
    samples = sorted(
        samples,
        key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name),
    )
    target = next((s for s in samples if s.path.name == target_name), None)
    if target is None:
        print(f"file {target_name} not found in dataset"); sys.exit(2)

    model = BackgroundModel(cfg)
    for s in samples:
        if s.path == target.path:
            break
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame)
        was_warmup = model.warmup_remaining(s.hour_bucket) > 0
        model.observe(s.hour_bucket)
        pred = classify(feats["mean"], s.hour_bucket, model, cfg)
        if oracle:
            if s.label == "cloud":
                model.update(s.hour_bucket, feats["mean"])
        else:
            if was_warmup or pred.label == "cloud":
                model.update(s.hour_bucket, feats["mean"])

    frame = load_gray_vga(target.path)
    feats = extract_tile_features(frame)
    model.observe(target.hour_bucket)
    result = classify(feats["mean"], target.hour_bucket, model, cfg)

    print(f"file        : {target.path.name}")
    print(f"true label  : {target.label}")
    print(f"prediction  : {result.label}")
    print(f"hour        : {target.hour_bucket}  (bucket {model._idx(target.hour_bucket)})")
    print(f"blob_max    : {result.blob_max_size}")
    print(f"anomaly_ratio = {result.anomaly_ratio:.3f}")
    print(f"compactness   = {result.compactness:.3f}")
    print(f"warmup        : {result.warmup}")
    print(f"reason        : {result.reason}")

    # Render: grayscale frame with anomalous tiles outlined in red.
    img = Image.fromarray(frame, "L").convert("RGB")
    draw = ImageDraw.Draw(img)
    for gy in range(GRID_H):
        for gx in range(GRID_W):
            if result.anomaly_mask[gy, gx]:
                x0, y0 = gx * TILE_W, gy * TILE_H
                x1, y1 = x0 + TILE_W - 1, y0 + TILE_H - 1
                draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=2)

    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"inspect_{target.path.stem}.png"
    img.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

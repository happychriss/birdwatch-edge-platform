"""Standalone diagnostic for the two dark-example photos.

Runs each image through the full classifier pipeline with a fresh background
model (no prior observations) so the result is independent of any server state.

Usage (from /workspace/src/cloud-check):
    .venv/bin/python -m scripts.analyze_dark
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.background import BackgroundModel
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.features import extract_tile_features, load_gray_vga

DARK_DIR = Path(__file__).resolve().parents[3] / "training-data" / "dark-example"

PHOTOS = {
    "20260515_205637.jpg": "cloud (empty scene)",
    "20260515_205706.jpg": "non-cloud (pillow/bird)",
}


def analyze(path: Path, expected: str, model: BackgroundModel, cfg: Config,
            prev_tile_mean: np.ndarray | None) -> np.ndarray:
    frame = load_gray_vga(path)
    feats = extract_tile_features(frame)
    hour = 20  # 20:56 / 20:57

    bucket = model._idx(hour)
    was_warmup = model.warmup_remaining(hour) > 0
    model.observe(hour)
    result = classify(feats["mean"], hour, model, cfg, prev_tile_mean=prev_tile_mean)

    bucket_mean_tiles = model.mean[model._idx(hour)]
    delta = feats["mean"] - bucket_mean_tiles

    print(f"\n{'='*60}")
    print(f"File      : {path.name}")
    print(f"Expected  : {expected}")
    print(f"Predicted : {result.label}  ({result.trigger})")
    print(f"Reason    : {result.reason}")
    print(f"---")
    print(f"Frame global mean brightness : {feats['global_mean']:.1f} / 255")
    print(f"Frame tile-mean min/max/std  : {feats['mean'].min():.1f} / {feats['mean'].max():.1f} / {feats['mean'].std():.1f}")
    print(f"Bucket model mean (avg tile) : {bucket_mean_tiles.mean():.1f}")
    print(f"Delta vs model min/max       : {delta.min():.1f} / {delta.max():.1f}")
    print(f"Anomaly ratio : {result.anomaly_ratio:.3f}  (threshold ≤ {cfg.quiet_anomaly_ratio})")
    print(f"Blob max size : {result.blob_max_size} tiles")
    print(f"Dark tiles    : new_dark={result.new_dark_tiles}  (need ≥ {cfg.dark_object_min_tiles})")
    print(f"  dark_obj_min_delta={cfg.dark_object_min_delta}  temporal_dark_delta={cfg.temporal_dark_delta}")
    print(f"Warmup        : {result.warmup} (was_warmup={was_warmup})")

    # Model update (mirrors serve.py logic)
    if was_warmup or result.label == "cloud" or result.trigger == "SCENE_DRIFT":
        model.update(hour, feats["mean"])

    return feats["mean"]


def warmup_model_on_empty(cfg: Config, n: int = 8) -> tuple[BackgroundModel, np.ndarray]:
    """Pre-train the background model with n copies of the empty scene to simulate
    a warmed-up server that has already seen 8+ evening frames before these two photos."""
    empty_path = DARK_DIR / "20260515_205637.jpg"
    model = BackgroundModel(cfg)
    frame = load_gray_vga(empty_path)
    feats = extract_tile_features(frame)
    hour = 20
    for _ in range(n):
        model.observe(hour)
        model.update(hour, feats["mean"])
    return model, feats["mean"]


def main() -> None:
    cfg = Config()

    print("=" * 60)
    print("PASS 1: Fresh model (WARMUP behaviour — server just started)")
    print("=" * 60)
    model = BackgroundModel(cfg)
    print(f"  hour 20 → bucket {model._idx(20)}  (warmup needs {cfg.warmup_frames_per_bucket} obs)")
    prev: np.ndarray | None = None
    for fname, expected in PHOTOS.items():
        path = DARK_DIR / fname
        if path.exists():
            prev = analyze(path, expected, model, cfg, prev_tile_mean=prev)

    print("\n")
    print("=" * 60)
    print("PASS 2: Pre-warmed model (8 empty-scene frames fed first)")
    print("  Simulates: server saw 8 earlier evening captures, all empty")
    print("=" * 60)
    model2, empty_tile_mean = warmup_model_on_empty(cfg, n=8)
    prev2: np.ndarray | None = empty_tile_mean
    for fname, expected in PHOTOS.items():
        path = DARK_DIR / fname
        if path.exists():
            prev2 = analyze(path, expected, model2, cfg, prev_tile_mean=prev2)


if __name__ == "__main__":
    main()

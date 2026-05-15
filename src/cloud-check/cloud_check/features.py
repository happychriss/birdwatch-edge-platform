from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# Matches the on-device filter input: VGA 640x480 grayscale.
FRAME_W = 640
FRAME_H = 480

# 16x12 grid → 40x40 tiles. 192 tiles total.
GRID_W = 16
GRID_H = 12
TILE_W = FRAME_W // GRID_W
TILE_H = FRAME_H // GRID_H


def load_gray_vga(path: Path) -> np.ndarray:
    """Load a JPEG, convert to 8-bit grayscale, resize to VGA. Returns (H, W) uint8."""
    with Image.open(path) as im:
        gray = im.convert("L").resize((FRAME_W, FRAME_H), Image.Resampling.BILINEAR)
        return np.asarray(gray, dtype=np.uint8)


def extract_tile_features(frame: np.ndarray) -> dict[str, np.ndarray]:
    """Compute per-tile statistics on a (FRAME_H, FRAME_W) grayscale frame.

    Returns dict with arrays shaped (GRID_H, GRID_W):
        mean: float32 — average intensity per tile
        std:  float32 — intensity stddev per tile (texture proxy)
        global_mean: scalar float — frame-wide mean (illumination state)
    """
    assert frame.shape == (FRAME_H, FRAME_W), frame.shape
    f = frame.astype(np.float32)
    tiles = f.reshape(GRID_H, TILE_H, GRID_W, TILE_W).transpose(0, 2, 1, 3)
    tiles = tiles.reshape(GRID_H, GRID_W, TILE_H * TILE_W)
    mean = tiles.mean(axis=2)
    std = tiles.std(axis=2)
    return {
        "mean": mean,
        "std": std,
        "global_mean": float(f.mean()),
    }

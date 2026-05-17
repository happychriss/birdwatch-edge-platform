from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# Matches the on-device filter input: VGA 640x480 grayscale.
FRAME_W = 640
FRAME_H = 480

# Default tile grid: 20×15 = 300 tiles of 32×32 px.
# Matches QQVGA (160×120) lightcheck with 8×8-pixel tiles on device.
GRID_W = 20
GRID_H = 15
TILE_W = FRAME_W // GRID_W
TILE_H = FRAME_H // GRID_H


def load_gray_vga(path: Path) -> np.ndarray:
    """Load a JPEG, convert to 8-bit grayscale, resize to VGA. Returns (H, W) uint8."""
    with Image.open(path) as im:
        gray = im.convert("L").resize((FRAME_W, FRAME_H), Image.Resampling.BILINEAR)
        return np.asarray(gray, dtype=np.uint8)


def extract_tile_features(
    frame: np.ndarray,
    grid_w: int = GRID_W,
    grid_h: int = GRID_H,
) -> dict[str, np.ndarray]:
    """Compute per-tile statistics on a (FRAME_H, FRAME_W) grayscale frame.

    grid_w / grid_h override the default 16×12 grid.  Use 32×24 to simulate
    QVGA (320×240) lightcheck resolution — 4× more tiles, same tile pixel size.

    Returns dict with arrays shaped (grid_h, grid_w):
        mean: float32 — average intensity per tile
        std:  float32 — intensity stddev per tile (texture proxy)
        global_mean: scalar float — frame-wide mean (illumination state)
    """
    assert frame.shape == (FRAME_H, FRAME_W), frame.shape
    tile_h = FRAME_H // grid_h
    tile_w = FRAME_W // grid_w
    f = frame.astype(np.float32)
    # Crop to exact multiple in case FRAME dimensions aren't divisible
    f = f[:grid_h * tile_h, :grid_w * tile_w]
    tiles = f.reshape(grid_h, tile_h, grid_w, tile_w).transpose(0, 2, 1, 3)
    tiles = tiles.reshape(grid_h, grid_w, tile_h * tile_w)
    mean = tiles.mean(axis=2)
    std = tiles.std(axis=2)
    return {
        "mean": mean,
        "std": std,
        "global_mean": float(f.mean()),
    }

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# Matches the on-device filter input dimensions:
#   VGA 640x480 is the analysis-resolution surrogate for the JPEG decode pipeline.
#   The ESP captures UXGA JPEG and stream-decodes per-MCU into the same 20×15 tile grid;
#   averaging absorbs the resolution difference at the tile-mean level (the per-tile
#   mean is ±0.5 DN stable across UXGA/SVGA/VGA).
FRAME_W = 640
FRAME_H = 480

# Tile grid: 20×15 = 300 tiles of 32×32 px at VGA.
# Matches CC_TILES_X / CC_TILES_Y on the device.
GRID_W = 20
GRID_H = 15
TILE_W = FRAME_W // GRID_W
TILE_H = FRAME_H // GRID_H


def load_gray_vga(path: Path) -> np.ndarray:
    """Load a JPEG, convert to 8-bit grayscale, resize to VGA. Returns (H, W) uint8."""
    with Image.open(path) as im:
        gray = im.convert("L").resize((FRAME_W, FRAME_H), Image.Resampling.BILINEAR)
        return np.asarray(gray, dtype=np.uint8)


def load_yuv_vga(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load a JPEG, convert to BT.601 YCbCr (matches OV2640 YUV422 output), resize to VGA.

    PIL's "YCbCr" mode uses BT.601 with the standard +128 offset on Cb/Cr. The OV2640
    emits YUV422 in the same encoding, so the per-tile means computed here are
    directly comparable to what the ESP on-device JPEG decoder will produce after
    walking the same JPEG bytes.

    Returns
    -------
    y, u, v : each (FRAME_H, FRAME_W) uint8.
    """
    with Image.open(path) as im:
        ycbcr = im.convert("YCbCr").resize((FRAME_W, FRAME_H), Image.Resampling.BILINEAR)
        arr = np.asarray(ycbcr, dtype=np.uint8)  # (H, W, 3) — Y, Cb, Cr
    return arr[..., 0], arr[..., 1], arr[..., 2]


def extract_tile_features(
    frame: np.ndarray,
    grid_w: int = GRID_W,
    grid_h: int = GRID_H,
) -> dict[str, np.ndarray]:
    """Compute per-tile statistics on a (FRAME_H, FRAME_W) grayscale frame.

    Returns dict with arrays shaped (grid_h, grid_w):
        mean        : float32 — average Y per tile
        std         : float32 — Y stddev per tile (texture proxy)
        global_mean : scalar float — frame-wide Y mean (truncated to match ESP integer division)
    """
    assert frame.shape == (FRAME_H, FRAME_W), frame.shape
    tile_h = FRAME_H // grid_h
    tile_w = FRAME_W // grid_w
    f = frame.astype(np.float32)
    f = f[:grid_h * tile_h, :grid_w * tile_w]
    tiles = f.reshape(grid_h, tile_h, grid_w, tile_w).transpose(0, 2, 1, 3)
    tiles = tiles.reshape(grid_h, grid_w, tile_h * tile_w)
    mean = tiles.mean(axis=2)
    std = tiles.std(axis=2)
    return {
        "mean": mean,
        "std": std,
        "global_mean": int(f.mean()),
    }


def extract_tile_features_yuv(
    y: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    grid_w: int = GRID_W,
    grid_h: int = GRID_H,
) -> dict[str, np.ndarray]:
    """Compute per-tile Y/U/V means on full-resolution channels.

    Each channel is averaged independently over the tile area. global_mean is
    computed on Y to match the ESP-side photo-bucket selection.

    Returns dict with arrays shaped (grid_h, grid_w):
        mean_y, mean_u, mean_v : float32 — per-tile channel means
        std_y                  : float32 — per-tile Y stddev (texture proxy)
        global_mean            : scalar int — truncated frame Y mean (mirrors ESP int math)
    """
    assert y.shape == (FRAME_H, FRAME_W), y.shape
    assert u.shape == (FRAME_H, FRAME_W), u.shape
    assert v.shape == (FRAME_H, FRAME_W), v.shape

    tile_h = FRAME_H // grid_h
    tile_w = FRAME_W // grid_w
    crop_h = grid_h * tile_h
    crop_w = grid_w * tile_w

    def per_tile_mean(ch: np.ndarray) -> np.ndarray:
        f = ch.astype(np.float32)[:crop_h, :crop_w]
        t = f.reshape(grid_h, tile_h, grid_w, tile_w).transpose(0, 2, 1, 3)
        return t.reshape(grid_h, grid_w, tile_h * tile_w).mean(axis=2)

    def per_tile_std(ch: np.ndarray) -> np.ndarray:
        f = ch.astype(np.float32)[:crop_h, :crop_w]
        t = f.reshape(grid_h, tile_h, grid_w, tile_w).transpose(0, 2, 1, 3)
        return t.reshape(grid_h, grid_w, tile_h * tile_w).std(axis=2)

    return {
        "mean_y": per_tile_mean(y),
        "mean_u": per_tile_mean(u),
        "mean_v": per_tile_mean(v),
        "std_y":  per_tile_std(y),
        "global_mean": int(y.astype(np.float32).mean()),
    }

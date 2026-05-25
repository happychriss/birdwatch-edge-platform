from __future__ import annotations

import numpy as np

from .config import Config
from . import scene_buckets


# Canonical photo-bucket names. Mirrors the C enum bw_photo_bucket_t and the
# ESP-side BW_CAM_MODE_PHOTO_* values. Order matches the int indices used to
# address the model arrays.
PHOTO_BUCKETS: tuple[str, ...] = ("NORMAL", "BRIGHT", "LOWLIGHT")
_PHOTO_BUCKET_TO_IDX: dict[str, int] = {name: i for i, name in enumerate(PHOTO_BUCKETS)}


def photo_bucket_for(global_mean: float, cfg: Config | None = None) -> str:
    """Map a frame's global Y mean to its photo-bucket name.

    Mirrors the ESP firmware decision: BW_BRIGHT_PHOTO_THRESHOLD=160,
    BW_LOWLIGHT_PHOTO_THRESHOLD=80. Same thresholds in both places.
    """
    cfg = cfg or Config()
    if global_mean >= cfg.bright_photo_threshold:
        return "BRIGHT"
    if global_mean < cfg.lowlight_photo_threshold:
        return "LOWLIGHT"
    return "NORMAL"


def photo_bucket_idx(name: str) -> int:
    """Convert a photo-bucket name to its array index. Raises on unknown name."""
    return _PHOTO_BUCKET_TO_IDX[name]


class BackgroundModel:
    """Per-tile YUV running statistics, nested by (photo-bucket × scene-bucket).

    Storage shape per channel: (num_photo_buckets, num_scene_buckets, grid_h, grid_w).
    Today num_scene_buckets=1 (no clustering yet) — the inner axis exists so that
    when k-means runs per photo-bucket later, K_scene grows without an API change.

    Three channels (Y, U, V) maintained independently with the same EMA. The
    classifier decides which channels gate which stages (QUIET is Y-only;
    DARK_OBJ adds a chroma sanity gate).
    """

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        P = self.cfg.num_photo_buckets
        S = self.cfg.num_scene_buckets
        H = self.cfg.grid_h
        W = self.cfg.grid_w
        shape = (P, S, H, W)
        # Y centered at 128, U/V also at 128 (BT.601 with +128 offset). Initial mean is
        # neutral grey — callers should override with seed_from_corpus() before replay.
        self.mean_y = np.full(shape, 128.0, dtype=np.float32)
        self.mean_u = np.full(shape, 128.0, dtype=np.float32)
        self.mean_v = np.full(shape, 128.0, dtype=np.float32)
        self.var_y = np.full(shape, self.cfg.init_var, dtype=np.float32)
        self.var_u = np.full(shape, self.cfg.init_var, dtype=np.float32)
        self.var_v = np.full(shape, self.cfg.init_var, dtype=np.float32)
        self.count = np.zeros(shape, dtype=np.uint16)
        self.bucket_seen = np.zeros((P, S), dtype=np.uint16)

    # ── Bucket selection ──────────────────────────────────────────────────────

    def photo_bucket_for(self, global_mean: float) -> str:
        return photo_bucket_for(global_mean, self.cfg)

    def scene_bucket_for(self, photo_bucket_idx_: int, tile_mean_y: np.ndarray) -> int:
        """Return the scene-bucket index inside the given photo-bucket.

        Today K_scene=1 → always 0. When K_scene grows the scene_buckets module
        provides per-photo-bucket centroids and this returns the nearest.
        """
        return scene_buckets.bucket_for(photo_bucket_idx_, tile_mean_y)

    # ── Model update ──────────────────────────────────────────────────────────

    def observe(self, pb_idx: int, sb_idx: int) -> None:
        """Record that a frame was processed in this (photo,scene) cell, regardless
        of result. Counts down warmup faster than the EMA does."""
        if self.bucket_seen[pb_idx, sb_idx] < 65535:
            self.bucket_seen[pb_idx, sb_idx] = self.bucket_seen[pb_idx, sb_idx] + 1

    def update(
        self,
        pb_idx: int,
        sb_idx: int,
        tile_mean_y: np.ndarray,
        tile_mean_u: np.ndarray | None = None,
        tile_mean_v: np.ndarray | None = None,
    ) -> None:
        """Fold a new accepted-as-background frame into the cell's EMA model.

        Always updates Y. U and V are updated when provided — legacy (luma-only)
        frames simply pass None and the chroma channels stay at their last state.
        """
        alpha = self.cfg.ema_alpha
        floor = self.cfg.var_floor

        def _update(mean_arr: np.ndarray, var_arr: np.ndarray, x: np.ndarray) -> None:
            prev_mean = mean_arr[pb_idx, sb_idx]
            new_mean = (1.0 - alpha) * prev_mean + alpha * x
            residual = x - new_mean
            new_var = (1.0 - alpha) * var_arr[pb_idx, sb_idx] + alpha * (residual * residual)
            mean_arr[pb_idx, sb_idx] = new_mean
            var_arr[pb_idx, sb_idx] = np.maximum(new_var, floor)

        _update(self.mean_y, self.var_y, tile_mean_y)
        if tile_mean_u is not None:
            _update(self.mean_u, self.var_u, tile_mean_u)
        if tile_mean_v is not None:
            _update(self.mean_v, self.var_v, tile_mean_v)

        cnt = self.count[pb_idx, sb_idx].astype(np.int32) + 1
        self.count[pb_idx, sb_idx] = np.clip(cnt, 0, 65535).astype(np.uint16)

    # ── Queries ───────────────────────────────────────────────────────────────

    def z_scores_y(self, pb_idx: int, sb_idx: int, tile_mean_y: np.ndarray) -> np.ndarray:
        """Absolute Y z-score per tile vs the cell's Y model."""
        std = np.sqrt(self.var_y[pb_idx, sb_idx])
        return np.abs(tile_mean_y - self.mean_y[pb_idx, sb_idx]) / std

    def chroma_delta_sq(
        self,
        pb_idx: int,
        sb_idx: int,
        tile_mean_u: np.ndarray,
        tile_mean_v: np.ndarray,
    ) -> np.ndarray:
        """Squared chroma distance per tile vs the cell's (U, V) model.

        Returns ΔC² = ΔU² + ΔV². The squared form matches the on-device integer
        math and avoids sqrt on the inner loop.
        """
        du = tile_mean_u - self.mean_u[pb_idx, sb_idx]
        dv = tile_mean_v - self.mean_v[pb_idx, sb_idx]
        return du * du + dv * dv

    def warmup_remaining(self, pb_idx: int, sb_idx: int) -> int:
        """Frames remaining until this cell exits warmup.

        Uses the actual model-update count (count[...].max()), NOT the
        observation count (bucket_seen). A cell that only sees DUPLICATE or
        DARK_OBJ frames never gets real background updates — keeping it in
        WARMUP ensures the first clean frame forces an update and prevents the
        model from being permanently stuck at the default-128 seed.
        """
        need = self.cfg.warmup_frames_per_bucket
        updated = int(self.count[pb_idx, sb_idx].max())
        return max(0, need - updated)

    def reset_warmup(self, pb_idx: int, sb_idx: int) -> None:
        """Force the cell back into warmup — used after SCENE_DRIFT."""
        self.bucket_seen[pb_idx, sb_idx] = 0
        self.count[pb_idx, sb_idx] = 0  # warmup_remaining uses count, so reset it

    def seed_from_corpus(
        self,
        pb_idx: int,
        sb_idx: int,
        tile_mean_y: np.ndarray,
        tile_mean_u: np.ndarray | None = None,
        tile_mean_v: np.ndarray | None = None,
    ) -> None:
        """Directly set the model mean from a pre-computed corpus average.

        Called once before the EMA replay loop in the backfill / warm_live_model
        to replace the default-128 seed with a per-tile estimate derived from
        historical data. Does NOT increment count — the cell starts in WARMUP and
        EMA updates push it toward the true background from here.
        """
        self.mean_y[pb_idx, sb_idx] = tile_mean_y.reshape(self.cfg.grid_h, self.cfg.grid_w)
        if tile_mean_u is not None:
            self.mean_u[pb_idx, sb_idx] = tile_mean_u.reshape(self.cfg.grid_h, self.cfg.grid_w)
        if tile_mean_v is not None:
            self.mean_v[pb_idx, sb_idx] = tile_mean_v.reshape(self.cfg.grid_h, self.cfg.grid_w)

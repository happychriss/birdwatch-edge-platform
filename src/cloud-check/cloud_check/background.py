from __future__ import annotations

import numpy as np

from .config import Config
from .dataset import time_bucket
from . import scene_buckets


class BackgroundModel:
    """Per-tile running statistics, bucketed by lighting scenario.

    Layout mirrors the on-device NVS blob:
        mean   : (num_buckets, grid_h, grid_w) float32
        var    : (num_buckets, grid_h, grid_w) float32
        count  : (num_buckets, grid_h, grid_w) uint16
        bucket_seen : (num_buckets,) uint16  — total frames observed in this bucket

    Bucket assignment uses nearest-centroid on the full tile_mean vector.
    The K=4 centroids encode 4 canonical lighting scenarios computed offline.
    Call bucket_for(tile_mean) to get the bucket index before calling other methods.
    """

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        n = self.cfg.num_time_buckets   # still controls array shape (default 4 = K)
        shape = (n, self.cfg.grid_h, self.cfg.grid_w)
        self.mean = np.full(shape, 128.0, dtype=np.float32)
        self.var = np.full(shape, self.cfg.init_var, dtype=np.float32)
        self.count = np.zeros(shape, dtype=np.uint16)
        self.bucket_seen = np.zeros(n, dtype=np.uint16)

    # ── Bucket selection ──────────────────────────────────────────────────────

    def bucket_for(self, tile_mean: np.ndarray) -> int:
        """Return the scene-lighting bucket index (0-indexed) for a frame.

        Uses nearest-centroid on the full 300-element tile_mean vector.
        Falls back to time-bucket 0 if centroids are unavailable (e.g. K≠4).
        """
        if self.mean.shape[0] == scene_buckets.K:
            return scene_buckets.bucket_for(tile_mean)
        return 0

    def _idx(self, hour: int) -> int:
        """Legacy time-based bucket index. Used by analysis scripts only."""
        return time_bucket(
            int(hour),
            self.cfg.num_time_buckets,
            self.cfg.day_start_hour,
            self.cfg.day_end_hour,
        )

    # ── Model update ──────────────────────────────────────────────────────────

    def observe(self, bucket: int) -> None:
        """Record that a frame was processed in this bucket, regardless of result.
        Counts down warmup faster than the EMA does."""
        if self.bucket_seen[bucket] < 65535:
            self.bucket_seen[bucket] = self.bucket_seen[bucket] + 1

    def update(self, bucket: int, tile_mean: np.ndarray) -> None:
        """Fold a new accepted-as-background frame into the model."""
        alpha = self.cfg.ema_alpha
        prev_mean = self.mean[bucket]
        new_mean = (1.0 - alpha) * prev_mean + alpha * tile_mean
        residual = tile_mean - new_mean
        new_var = (1.0 - alpha) * self.var[bucket] + alpha * (residual * residual)
        self.mean[bucket] = new_mean
        self.var[bucket] = np.maximum(new_var, self.cfg.var_floor)
        cnt = self.count[bucket].astype(np.int32) + 1
        self.count[bucket] = np.clip(cnt, 0, 65535).astype(np.uint16)

    # ── Queries ───────────────────────────────────────────────────────────────

    def z_scores(self, bucket: int, tile_mean: np.ndarray) -> np.ndarray:
        std = np.sqrt(self.var[bucket])
        return np.abs(tile_mean - self.mean[bucket]) / std

    def warmup_remaining(self, bucket: int) -> int:
        """Observations still needed before this bucket leaves warmup mode."""
        need = self.cfg.warmup_frames_per_bucket
        seen = int(self.bucket_seen[bucket])
        return max(0, need - seen)

    def reset_warmup(self, bucket: int) -> None:
        """Force the bucket back into warmup — used after SCENE_DRIFT to
        re-bootstrap when the scene has changed significantly."""
        self.bucket_seen[bucket] = 0

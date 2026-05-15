from __future__ import annotations

import numpy as np

from .config import Config
from .dataset import time_bucket
from .features import GRID_H, GRID_W


class BackgroundModel:
    """Per-tile running statistics, bucketed by day-period.

    Layout mirrors the planned on-device NVS blob:
        mean   : (num_buckets, GRID_H, GRID_W) float32
        var    : (num_buckets, GRID_H, GRID_W) float32
        count  : (num_buckets, GRID_H, GRID_W) uint16
        bucket_seen : (num_buckets,) uint16  — total frames observed in this bucket

    Default storage on the device for 4 buckets × 12 × 16 tiles:
        4 × 192 × (4+4+2) + 4×2  ≈ 7 700 bytes
    """

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        n = self.cfg.num_time_buckets
        shape = (n, GRID_H, GRID_W)
        self.mean = np.full(shape, 128.0, dtype=np.float32)
        self.var = np.full(shape, self.cfg.init_var, dtype=np.float32)
        self.count = np.zeros(shape, dtype=np.uint16)
        self.bucket_seen = np.zeros(n, dtype=np.uint16)

    def _idx(self, hour: int) -> int:
        return time_bucket(
            int(hour),
            self.cfg.num_time_buckets,
            self.cfg.day_start_hour,
            self.cfg.day_end_hour,
        )

    def observe(self, hour: int) -> None:
        """Record that we processed a frame in this bucket, regardless of how
        we classified it. Used to count down warmup faster than the EMA does."""
        b = self._idx(hour)
        if self.bucket_seen[b] < 65535:
            self.bucket_seen[b] = self.bucket_seen[b] + 1

    def update(self, hour: int, tile_mean: np.ndarray) -> None:
        """Fold a new accepted-as-cloud frame into the model."""
        b = self._idx(hour)
        alpha = self.cfg.ema_alpha
        prev_mean = self.mean[b]
        new_mean = (1.0 - alpha) * prev_mean + alpha * tile_mean
        residual = tile_mean - new_mean
        new_var = (1.0 - alpha) * self.var[b] + alpha * (residual * residual)
        self.mean[b] = new_mean
        self.var[b] = np.maximum(new_var, self.cfg.var_floor)
        cnt = self.count[b].astype(np.int32) + 1
        self.count[b] = np.clip(cnt, 0, 65535).astype(np.uint16)

    def z_scores(self, hour: int, tile_mean: np.ndarray) -> np.ndarray:
        b = self._idx(hour)
        std = np.sqrt(self.var[b])
        return np.abs(tile_mean - self.mean[b]) / std

    def warmup_remaining(self, hour: int) -> int:
        """How many more *observations* the bucket needs before strict mode."""
        b = self._idx(hour)
        need = self.cfg.warmup_frames_per_bucket
        seen = int(self.bucket_seen[b])
        return max(0, need - seen)

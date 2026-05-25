"""Scene-bucket centroids — currently degenerate K=1 placeholder.

Background
----------
The previous K=4 centroids (gm 56 / 102 / 118 / 154) actually clustered on
brightness, which the new design encodes via the outer photo-bucket layer
(NORMAL / BRIGHT / LOWLIGHT). The inner scene-bucket axis is for shadow-pattern
clusters *within* a single photo-bucket and starts at K=1 — one cell per
photo-bucket, populated directly from RTC reference frames.

When enough YUV+RTC data exists per photo-bucket, k-means inside each photo-bucket
will produce per-photo-bucket centroid arrays (see compute_centroids.py). At that
point this file gets per-photo-bucket centroid constants and K_scene grows.

For K_scene=1, bucket_for() always returns 0 — no clustering, no centroid math.
"""
from __future__ import annotations

import numpy as np


K = 1   # K_scene per photo-bucket.  Forward-compatible: grows when k-means runs.


def bucket_for(photo_bucket_idx: int, tile_mean_y: np.ndarray) -> int:
    """Return scene-bucket index within the given photo-bucket.

    K=1 today → always 0. Signature is forward-compatible: when K>1 lands,
    this function will dispatch to per-photo-bucket centroid arrays and return
    the nearest-centroid index inside that photo-bucket's cluster set.
    """
    return 0

"""compute_centroids.py — Compute K=4 lighting-scenario bucket centroids.

Reads tile_means from the production DB (background frames only), runs K-means,
and prints the centroids as Python and C constants for baking into the codebase.

Usage:
    cd /workspace/src/cloud-check
    python compute_centroids.py [--k 4] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_here = Path(__file__).parent
_server_dir = _here.parent / 'python_bw_src'
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_server_dir))

from dotenv import load_dotenv
load_dotenv(_server_dir / '.env')

import numpy as np
from sklearn.cluster import KMeans
from db import BwFrame, Session

# Labels that indicate a real foreground object — exclude from centroid training.
_FOREGROUND_LABELS = {'birds', 'bird', 'person', 'people', 'pillow', 'process', 'object'}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--k', type=int, default=4, help='Number of clusters (default 4)')
    ap.add_argument('--dry-run', action='store_true', help='Analyse without printing C constants')
    args = ap.parse_args()
    K = args.k

    session = Session()
    frames = (session.query(BwFrame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.asc())
              .all())

    tile_means_list = []
    skipped = 0
    for f in frames:
        meta = f.meta or {}
        label = (meta.get('label') or '').lower()
        if label in _FOREGROUND_LABELS:
            skipped += 1
            continue
        tm = meta.get('tile_means')
        if tm is None or len(tm) != 300:
            skipped += 1
            continue
        tile_means_list.append(tm)

    X = np.array(tile_means_list, dtype=np.float32)
    print(f"Background frames: {len(X)}  (skipped {skipped})")
    print(f"Overall per-tile std: {X.std(axis=0).mean():.2f} DN")

    km = KMeans(n_clusters=K, n_init=20, random_state=42, max_iter=500)
    km.fit(X)
    labels = km.labels_
    centroids = km.cluster_centers_  # (K, 300)

    # Sort clusters by mean brightness (ascending) for stable ordering
    brightness = centroids.mean(axis=1)
    order = np.argsort(brightness)
    centroids = centroids[order]
    new_labels = np.empty_like(labels)
    for new_k, old_k in enumerate(order):
        new_labels[labels == old_k] = new_k
    labels = new_labels

    print(f"\nK={K} clusters (sorted by brightness):")
    weighted_std = 0.0
    for k in range(K):
        mask = labels == k
        members = X[mask]
        per_tile_std = members.std(axis=0).mean()
        gm = centroids[k].mean()
        weighted_std += per_tile_std * mask.sum() / len(X)
        print(f"  Bucket {k}: n={mask.sum():3d}  mean_gm={gm:.1f}  per_tile_std={per_tile_std:.2f} DN")
    print(f"Weighted per-tile std: {weighted_std:.2f} DN  (baseline {X.std(axis=0).mean():.2f})")

    if args.dry_run:
        return

    # ── Python constants ──────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("# scene_buckets.py — paste into cloud_check/scene_buckets.py")
    print("=" * 72)
    print("CENTROIDS = np.array([")
    for k in range(K):
        c = centroids[k]
        vals = ", ".join(f"{v:.2f}" for v in c)
        gm = c.mean()
        print(f"    # bucket {k}: n={int((labels==k).sum())} gm={gm:.1f}")
        print(f"    [{vals}],")
    print("], dtype=np.float32)")

    # ── C constants ───────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("// cloud_check.c — paste into CC_CENTROIDS constant")
    print("=" * 72)
    print(f"static const float CC_CENTROIDS[{K}][CC_NUM_TILES] = {{")
    for k in range(K):
        c = centroids[k]
        # Print 10 values per line
        print(f"    /* bucket {k}: n={int((labels==k).sum())} gm={c.mean():.1f} */")
        print("    {", end="")
        for i, v in enumerate(c):
            if i > 0 and i % 10 == 0:
                print("\n     ", end="")
            print(f"{v:.2f}f", end="")
            if i < len(c) - 1:
                print(", ", end="")
        print("},")
    print("};")


if __name__ == '__main__':
    main()

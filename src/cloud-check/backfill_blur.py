"""backfill_blur.py — Experimental high-pass (blur) layer-2 detector.

Piggybacks on an already-run backfill_meta.py: reads existing tile_means
and model_tile_means from the DB (no JPG fetch needed), applies a box-blur
high-pass to remove slow illumination gradients, then runs a dead-simple
threshold detector.

High-pass decomposition (per frame):
    HF(X)[i] = X[i] − blur(X, k)[i]
    hp_delta[i] = HF(model)[i] − HF(tile)[i]
              = (model[i] − tile[i]) − (blur(model)[i] − blur(tile)[i])

Positive hp_delta = tile is a LOCAL dark anomaly versus the model.
Diffuse illumination shifts (cloud shadow, sunset gradient) are low-frequency
and cancel out; compact objects (birds) survive.

Layer-2 rule:
    DARK_BLOB_HP  if hp_delta.max() >= threshold  → process
    QUIET_HP      otherwise                        → clouds

Layer-1 (burst filter) results are taken straight from the existing meta —
no re-run needed.

Stored fields (added/overwritten):
    result, stage           — HP-based decision
    model_tile_means        — HP-adjusted: model_adj = HF(model) + blur(tile)
                              so that overlay Δm = hp_delta directly
    tile_delta_luma         — hp_delta flattened (shown as tooltip/text)
    hp_score                — max(hp_delta) across all tiles
    hp_blur_k               — kernel size used
    simulated_blur          — True marker to distinguish from regular backfill

Usage:
    python backfill_blur.py                          # all frames with tile data
    python backfill_blur.py --from-frame 511         # from frame id >= 511
    python backfill_blur.py --threshold 25 --blur-k 5
    python backfill_blur.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_here = Path(__file__).parent
sys.path.insert(0, str(_here))
_server_dir = _here.parent / 'python_bw_src'
sys.path.insert(0, str(_server_dir))

from dotenv import load_dotenv
load_dotenv(_server_dir / '.env')

import numpy as np
from scipy.ndimage import uniform_filter
from sqlalchemy.orm.attributes import flag_modified

from cloud_check.pipeline import BURST_SUPPRESS_STAGES
from db import BwFrame, Session

GRID_H, GRID_W = 15, 20


def box_blur(arr: np.ndarray, k: int) -> np.ndarray:
    return uniform_filter(arr.astype(float), size=k, mode='nearest')


def hp_delta(tile: np.ndarray, model: np.ndarray, k: int) -> np.ndarray:
    """High-pass residual: HF(model) - HF(tile). Positive = locally darker than model."""
    return (model - box_blur(model, k)) - (tile - box_blur(tile, k))


def run(
    dry_run: bool = False,
    from_frame: int | None = None,
    threshold: float = 25.0,
    blur_k: int = 5,
) -> None:
    session = Session()

    q = session.query(BwFrame).order_by(BwFrame.id)
    if from_frame is not None:
        q = q.filter(BwFrame.id >= from_frame)
    frames = q.all()

    total = len(frames)
    skipped = updated = 0

    print(f"Frames to process: {total}  (threshold={threshold} DN, blur_k={blur_k})")

    for frame in frames:
        meta = frame.meta or {}

        tm_flat  = meta.get('tile_means')
        mdl_flat = meta.get('model_tile_means')
        if not tm_flat or not mdl_flat:
            skipped += 1
            continue

        T   = np.array(tm_flat,  dtype=float).reshape(GRID_H, GRID_W)
        M   = np.array(mdl_flat, dtype=float).reshape(GRID_H, GRID_W)
        hpd = hp_delta(T, M, blur_k)         # (H, W), +ve = locally darker

        hp_score = float(hpd.max())

        # ── Layer-1: reuse existing burst result ─────────────────────────────
        burst = meta.get('burst_trigger', 'UNKNOWN')
        if burst in BURST_SUPPRESS_STAGES:
            # burst already suppressed; keep as-is, don't overwrite
            skipped += 1
            continue

        # ── Layer-2: HP threshold ─────────────────────────────────────────────
        if hp_score >= threshold:
            stage  = 'DARK_BLOB_HP'
            result = 'process'
        else:
            stage  = 'QUIET_HP'
            result = 'clouds'

        # ── HP-adjusted model means so overlay shows Δm = hp_delta ────────────
        # model_adj[i] = HF(model)[i] + blur(tile)[i]
        # → overlay computes: model_adj - tile = HF(model) - HF(tile) = hp_delta
        T_lo    = box_blur(T, blur_k)
        M_hi    = M - box_blur(M, blur_k)
        model_adj = M_hi + T_lo              # (H, W)

        ts = frame.captured_at.strftime('%Y-%m-%d %H:%M') if frame.captured_at else '?'
        src = meta.get('source', '?')
        lbl = meta.get('label', '')
        print(f"  {'DRY' if dry_run else 'UPD'} {frame.filename}  {ts} [{src}]"
              f"  burst={burst:<17} hp_score={hp_score:5.1f}  stage={stage:<12}  result={result}"
              f"{'  label='+lbl if lbl else ''}")

        if not dry_run:
            new_fields = {
                'result':           result,
                'stage':            stage,
                'model_tile_means': model_adj.flatten().round(1).tolist(),
                'tile_delta_luma':  hpd.flatten().round(1).tolist(),
                'hp_score':         round(hp_score, 1),
                'hp_blur_k':        blur_k,
                'simulated_blur':   True,
            }
            patched = {**meta, **new_fields}
            frame.meta   = patched
            frame.result = result
            flag_modified(frame, 'meta')
            updated += 1

    if not dry_run and updated > 0:
        session.commit()
        print(f'\nCommitted {updated} rows.')

    print(f'\nDone: {updated} updated, {skipped} skipped (no tile data or burst-suppressed)')
    session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description='High-pass blur layer-2 experiment')
    ap.add_argument('--dry-run',    action='store_true')
    ap.add_argument('--from-frame', type=int, metavar='ID')
    ap.add_argument('--threshold',  type=float, default=25.0,
                    help='HP delta threshold DN for DARK_BLOB_HP (default 25)')
    ap.add_argument('--blur-k',     type=int, default=5,
                    help='Box-blur kernel size in tiles (default 5)')
    args = ap.parse_args()
    run(dry_run=args.dry_run,
        from_frame=args.from_frame,
        threshold=args.threshold,
        blur_k=args.blur_k)


if __name__ == '__main__':
    main()

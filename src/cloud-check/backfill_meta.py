"""backfill_meta.py — Back-fill pipeline results for old bw_frames rows.

Adds burst_trigger / burst_label / burst_n_* fields (and, where missing,
result / stage / global_mean / photo_mode) to every bw_frames row that has
a JPEG file but was captured before the burst-filter firmware flash (2026-05-23 09:42).

Frames that already carry burst_trigger in meta are treated as authoritative
(new firmware) and are not overwritten.  Their image data is still fed into the
running burst/bg-model state so the simulation stays in sync.

Usage:
    python backfill_meta.py [--dry-run] [--force]

    --dry-run   Print what would change; do not touch the database.
    --force     Re-compute and overwrite even frames that already have burst_trigger.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# cloud_check lives in the same directory
_here = Path(__file__).parent
sys.path.insert(0, str(_here))

# db.py (SQLAlchemy models + connection) lives in ../python_bw_src/
_server_dir = _here.parent / 'python_bw_src'
sys.path.insert(0, str(_server_dir))

from dotenv import load_dotenv
load_dotenv(_server_dir / '.env')

import numpy as np
from sqlalchemy.orm.attributes import flag_modified

from cloud_check.burst_filter import BurstConfig, burst_classify
from cloud_check.background import BackgroundModel
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.features import load_gray_vga, extract_tile_features
from db import BwFrame, Session

_jpg_raw = os.getenv('JPG_FOLDER_PATH', '/tmp')
JPG_FOLDER = (_server_dir / _jpg_raw).resolve() if not Path(_jpg_raw).is_absolute() else Path(_jpg_raw)


def _photo_mode(gm: int) -> str:
    if gm < 130:
        return 'LOWLIGHT'
    return 'NORMAL'


def run_backfill(dry_run: bool = False, force: bool = False) -> None:
    session = Session()

    frames = (session.query(BwFrame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.asc())
              .all())

    print(f"Found {len(frames)} frames with filenames", flush=True)

    # Config mirrors firmware: single time bucket, 4-frame warmup.
    cc_cfg = Config(num_time_buckets=1, warmup_frames_per_bucket=4)
    burst_cfg = BurstConfig()
    bg_model = BackgroundModel(cc_cfg)

    prev_tile_mean: np.ndarray | None = None
    prev_gm: float | None = None
    prev_ts: datetime | None = None
    prev_tile_mean_by_bucket: dict[int, np.ndarray] = {}

    updated = skipped = errors = 0

    for frame in frames:
        meta = dict(frame.meta or {})
        already_authoritative = 'burst_trigger' in meta

        jpg_path = JPG_FOLDER / frame.filename
        if not jpg_path.exists():
            print(f"  MISS  {frame.filename}", flush=True)
            errors += 1
            continue

        try:
            gray = load_gray_vga(jpg_path)
            feats = extract_tile_features(gray)
        except Exception as exc:
            print(f"  ERR   {frame.filename}: {exc}", flush=True)
            errors += 1
            continue

        tile_mean: np.ndarray = feats['mean']   # (GRID_H, GRID_W) float32
        gm: int = feats['global_mean']           # truncated int, matches ESP

        # dt since previous frame
        if prev_ts is not None and frame.captured_at is not None:
            dt = max(0.0, (frame.captured_at - prev_ts).total_seconds())
        else:
            dt = float('inf')

        # --- Run pipeline for state (always, even if we skip writing) ---

        burst = burst_classify(
            tile_mean, float(gm),
            prev_tile_mean, prev_gm,
            dt, burst_cfg,
        )

        hour = frame.captured_at.hour if frame.captured_at else 12
        bucket = bg_model._idx(hour)
        bg_prev = prev_tile_mean_by_bucket.get(bucket)
        was_warmup = bg_model.warmup_remaining(hour) > 0
        bg_model.observe(hour)

        if burst.label == 'suppress':
            result = 'clouds'
            stage = burst.trigger
            bg_pred = None
        else:
            bg_pred = classify(tile_mean, hour, bg_model, cc_cfg, prev_tile_mean=bg_prev)
            result = bg_pred.label
            stage = bg_pred.trigger
            if was_warmup or bg_pred.label == 'clouds' or bg_pred.trigger in ('SCENE_DRIFT', 'NIGHT'):
                bg_model.update(hour, tile_mean)
            if bg_pred.trigger == 'SCENE_DRIFT':
                bg_model.reset_warmup(hour)
            prev_tile_mean_by_bucket[bucket] = tile_mean

        # Always advance burst state
        prev_tile_mean = tile_mean
        prev_gm = float(gm)
        prev_ts = frame.captured_at

        if already_authoritative and not force:
            skipped += 1
            continue

        # --- Build patched meta ---
        new_fields: dict = {
            'burst_trigger':   burst.trigger,
            'burst_label':     burst.label,
            'burst_gm_diff':   round(burst.gm_diff, 1),
            'burst_n_changed': burst.n_changed,
            'burst_n_dark':    burst.n_dark,
            'result':          result,
            'stage':           stage,
            'global_mean':     gm,
            'simulated':       True,
        }
        if 'photo_mode' not in meta:
            new_fields['photo_mode'] = _photo_mode(gm)
        if bg_pred is not None:
            new_fields['ratio'] = round(bg_pred.anomaly_ratio, 3)
            new_fields['new_dark_tiles'] = bg_pred.new_dark_tiles
            new_fields['dark_anomalous'] = int(bg_pred.anomaly_mask.sum())

        patched = {**meta, **new_fields}

        ts_str = frame.captured_at.strftime('%Y-%m-%d %H:%M') if frame.captured_at else '?'
        print(f"  {'DRY' if dry_run else 'UPD'} {frame.filename} {ts_str}"
              f"  burst={burst.trigger}  bg={stage}  result={result}", flush=True)

        if not dry_run:
            frame.meta = patched
            flag_modified(frame, 'meta')
            frame.result = result
            updated += 1

    if not dry_run and updated > 0:
        session.commit()
        print('\nCommitted.', flush=True)

    print(f'\nDone: {updated} updated, {skipped} skipped (authoritative), {errors} errors',
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description='Back-fill burst pipeline meta for old bw_frames rows')
    ap.add_argument('--dry-run', action='store_true', help='Print changes without writing to DB')
    ap.add_argument('--force', action='store_true',
                    help='Overwrite even frames that already have burst_trigger')
    args = ap.parse_args()
    run_backfill(dry_run=args.dry_run, force=args.force)


if __name__ == '__main__':
    main()

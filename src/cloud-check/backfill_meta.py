"""backfill_meta.py — Clean canonical recompute of pipeline meta from JPGs.

For every bw_frames row that has a JPG file, this script:

  1. Loads the JPG and extracts tile_means + global_mean directly from it
     (so the stored telemetry matches the visible image — not the QQVGA
     lightcheck capture that the firmware made a moment earlier).
  2. Replays the burst pre-filter + background-model pipeline against the
     JPG-derived tile_means, in strict chronological order, so the bg model
     evolves consistently across the dataset.
  3. Snapshots the model means BEFORE each frame's update and stores them
     as model_tile_means (drives the Δm tile overlay in the detail view).
  4. Overwrites: tile_means, model_tile_means, result, stage, global_mean,
     ratio, dark_anomalous, dark_tiles, new_dark_tiles, photo_mode,
     burst_trigger, burst_label, burst_gm_diff, burst_n_changed, burst_n_dark.
     Marks every reprocessed row with simulated=True.
  5. Preserves manual / external markers: label, downloaded_at, fresh_flash,
     fw_build (and any other meta keys the script does not touch).

Frames are processed unconditionally — the previous "already authoritative"
skip is gone.  When new firmware uploads land via /frame, the server keeps
the ESP-provided values; only this script overwrites them.

Usage:
    python backfill_meta.py [--dry-run]
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
from cloud_check.scene_buckets import CENTROIDS, K as CC_K
from db import BwFrame, Session

# Burst stages that suppress without running the background model.
# DIFFUSE and dt-based stages (FAST_SHIFT, ISOLATED) fall through to the BG
# model — the BG model is better-positioned to decide (compares vs long-term
# mean, not just prev frame).
_BURST_SUPPRESS_STAGES = frozenset({'DUPLICATE', 'BRIGHT_STABLE'})

_jpg_raw = os.getenv('JPG_FOLDER_PATH', '/tmp')
JPG_FOLDER = (_server_dir / _jpg_raw).resolve() if not Path(_jpg_raw).is_absolute() else Path(_jpg_raw)

# Fields that this script recomputes and overwrites every run.
_OVERWRITE_KEYS = (
    'tile_means', 'model_tile_means', 'result', 'stage', 'global_mean',
    'ratio', 'dark_anomalous', 'dark_tiles', 'new_dark_tiles', 'dark_blob_max', 'photo_mode',
    'burst_trigger', 'burst_label', 'burst_gm_diff', 'burst_n_changed',
    'burst_n_dark', 'warmup', 'prev_valid', 'simulated',
)


def _photo_mode(gm: int) -> str:
    if gm < 130:
        return 'LOWLIGHT'
    return 'NORMAL'


def run_backfill(dry_run: bool = False) -> None:
    session = Session()

    frames = (session.query(BwFrame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.asc())
              .all())

    print(f"Found {len(frames)} frames with filenames", flush=True)
    print(f"JPG folder: {JPG_FOLDER}", flush=True)

    # Match firmware exactly: K=4 lighting-scenario buckets.
    # warmup_frames_per_bucket=4 to match ESP CC_WARMUP_FRAMES; centroids pre-seed the means.
    cc_cfg = Config(num_time_buckets=CC_K, warmup_frames_per_bucket=4)
    burst_cfg = BurstConfig()
    bg_model = BackgroundModel(cc_cfg)
    # Pre-seed each bucket's mean from the computed centroids — skip the mean=128 cold start.
    for _b in range(CC_K):
        bg_model.mean[_b] = CENTROIDS[_b].reshape(cc_cfg.grid_h, cc_cfg.grid_w).copy()
        bg_model.bucket_seen[_b] = cc_cfg.warmup_frames_per_bucket  # centroid counts as warmup

    prev_tile_mean: np.ndarray | None = None
    prev_gm: float | None = None
    prev_ts: datetime | None = None
    prev_tile_mean_by_bucket: dict[int, np.ndarray] = {}

    updated = errors = 0

    for frame in frames:
        meta = dict(frame.meta or {})

        # Prefer stored tile_means from DB (written by a previous backfill or by the
        # firmware).  This lets the simulation run without local JPG access — the
        # production server's photos are only needed on first population.
        stored_tm = meta.get('tile_means')
        expected_tiles = cc_cfg.grid_h * cc_cfg.grid_w
        if stored_tm and isinstance(stored_tm, list) and len(stored_tm) == expected_tiles:
            tile_mean = np.array(stored_tm, dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
            gm = int(float(tile_mean.mean()))  # truncate, matches ESP integer division
        else:
            # Fall back to loading from local disk or HTTP
            jpg_path = JPG_FOLDER / frame.filename
            if jpg_path.exists():
                try:
                    gray = load_gray_vga(jpg_path)
                    feats = extract_tile_features(gray)
                    tile_mean = feats['mean']
                    gm = feats['global_mean']
                except Exception as exc:
                    print(f"  ERR   {frame.filename}: {exc}", flush=True)
                    errors += 1
                    continue
            else:
                # Try HTTP fetch from production server
                server_base = os.getenv('PHOTO_SERVER', 'http://192.168.1.110:8000').rstrip('/')
                url = f"{server_base}/static/{frame.filename}"
                try:
                    import io, requests
                    from PIL import Image as PILImage
                    resp = requests.get(url, timeout=15)
                    resp.raise_for_status()
                    img = PILImage.open(io.BytesIO(resp.content)).convert('L').resize((640, 480))
                    frame_arr = np.asarray(img, dtype=np.uint8)
                    feats = extract_tile_features(frame_arr)
                    tile_mean = feats['mean']
                    gm = feats['global_mean']
                except Exception as exc:
                    print(f"  MISS  {frame.filename}: {exc}", flush=True)
                    errors += 1
                    continue

        # dt since previous frame
        if prev_ts is not None and frame.captured_at is not None:
            dt = max(0.0, (frame.captured_at - prev_ts).total_seconds())
        else:
            dt = float('inf')

        # --- Burst pre-filter ----------------------------------------------------
        burst = burst_classify(
            tile_mean, float(gm),
            prev_tile_mean, prev_gm,
            dt, burst_cfg,
        )

        hour = frame.captured_at.hour if frame.captured_at else 12
        bucket = bg_model.bucket_for(tile_mean)
        bg_prev = prev_tile_mean_by_bucket.get(bucket)
        was_warmup = bg_model.warmup_remaining(bucket) > 0
        bg_model.observe(bucket)

        # Snapshot model means BEFORE any update — what z-scores were computed from
        model_means_flat: list[int] = bg_model.mean[bucket].flatten().round().astype(int).tolist()

        bg_pred = None
        burst_suppresses = (burst.label == 'suppress'
                            and burst.trigger in _BURST_SUPPRESS_STAGES)
        if burst_suppresses:
            result = 'clouds'
            stage = burst.trigger
        else:
            bg_pred = classify(tile_mean, hour, bg_model, cc_cfg, prev_tile_mean=bg_prev)
            result = bg_pred.label
            stage = bg_pred.trigger
            if was_warmup or bg_pred.label == 'clouds' or bg_pred.trigger in ('SCENE_DRIFT', 'NIGHT'):
                bg_model.update(bucket, tile_mean)
            if bg_pred.trigger == 'SCENE_DRIFT':
                bg_model.reset_warmup(bucket)
            prev_tile_mean_by_bucket[bucket] = tile_mean

        # Advance burst state for next iteration
        prev_tile_mean = tile_mean
        prev_gm = float(gm)
        prev_ts = frame.captured_at

        # --- Build the canonical meta payload ----------------------------------
        tile_means_flat: list[int] = tile_mean.flatten().round().astype(int).tolist()

        new_fields: dict = {
            'tile_means':       tile_means_flat,
            'model_tile_means': model_means_flat,
            'global_mean':      gm,
            'photo_mode':       _photo_mode(gm),
            'result':           result,
            'stage':            stage,
            'warmup':           bool(was_warmup),
            'prev_valid':       bool(bg_prev is not None),
            'burst_trigger':    burst.trigger,
            'burst_label':      burst.label,
            'burst_gm_diff':    round(burst.gm_diff, 1),
            'burst_n_changed':  int(burst.n_changed),
            'burst_n_dark':     int(burst.n_dark),
            'simulated':        True,
        }
        if bg_pred is not None:
            new_fields['ratio']           = round(float(bg_pred.anomaly_ratio), 3)
            new_fields['new_dark_tiles']  = int(bg_pred.new_dark_tiles)
            new_fields['dark_anomalous']  = int(bg_pred.anomaly_mask.sum())
            new_fields['dark_tiles']      = int(bg_pred.dark_tiles)
            new_fields['dark_blob_max']   = int(bg_pred.dark_blob_max)
            new_fields['scene_bucket']    = int(bg_pred.scene_bucket)
        else:
            # Burst-suppressed: no bg model output, zero out the bg-only stats
            new_fields['ratio']          = 0.0
            new_fields['new_dark_tiles'] = 0
            new_fields['dark_anomalous'] = 0
            new_fields['dark_tiles']     = 0
            new_fields['dark_blob_max']  = 0

        # Merge: existing meta wins for manual/external keys (label, downloaded_at,
        # fresh_flash, fw_build, etc.); recomputed keys overwrite.
        patched = {**meta, **new_fields}

        ts_str = frame.captured_at.strftime('%Y-%m-%d %H:%M') if frame.captured_at else '?'
        print(f"  {'DRY' if dry_run else 'UPD'} {frame.filename} {ts_str}"
              f"  burst={burst.trigger:<17} bg={stage:<12} result={result}", flush=True)

        if not dry_run:
            frame.meta = patched
            flag_modified(frame, 'meta')
            frame.result = result
            updated += 1

    if not dry_run and updated > 0:
        session.commit()
        print('\nCommitted.', flush=True)

    print(f'\nDone: {updated} updated, {errors} errors (missing JPG / decode fail)',
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description='Clean canonical recompute of pipeline meta from JPGs')
    ap.add_argument('--dry-run', action='store_true', help='Print changes without writing to DB')
    # Accepted for backwards-compat with /admin/backfill endpoint; no longer meaningful
    # (every run unconditionally reprocesses every frame with a JPG).
    ap.add_argument('--force', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args()
    run_backfill(dry_run=args.dry_run)


if __name__ == '__main__':
    main()

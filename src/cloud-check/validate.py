"""RETIRED 2026-09-05 — kept for reference only, do not run.

This assumes the per-tile background model (EMA z-score over photo buckets,
stages WARMUP / DARK_BLOB / QUIET / AMBIGUOUS).  That model was removed from the
firmware after measurement showed it could not separate birds on this scene:
32% recall at a 10% false-positive rate, against a requirement of 100%.

Suppression now happens before the camera is powered, from the clock alone —
solar elevation, quiet gap, burst position.  See presuppress_model.py, which
fits that rule and exports the lookup table the firmware uses, and experiment.md
for how the conclusion was reached.
"""
"""
validate.py — ESP-vs-Python parity validator.

Driven entirely by validate_config.json.  To add or remove a checked value,
edit the "checks" array in the config — no code change needed here.

Run standalone:
    cd src/cloud-check
    python validate.py [path/to/validate_config.json]

Or triggered from the Flask server via POST /validate/run (reads the same config).

Returns a list of per-frame dicts.  Each dict has:
    frame_id    : int
    captured_at : str (ISO)
    mismatches  : list of {key, esp_val, py_val, delta}

Frames with zero mismatches are included (empty mismatches list) so the caller
knows how many frames were actually checked.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

# ── locate the cloud_check package and db module ─────────────────────────────
_here = Path(__file__).parent
_server_dir = _here.parent / 'python_bw_src'

if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
if str(_server_dir) not in sys.path:
    sys.path.insert(0, str(_server_dir))

from cloud_check.background import BackgroundModel, photo_bucket_idx
from cloud_check.burst_filter import BurstConfig, burst_classify
from cloud_check.classifier import classify, ClassifierResult
from cloud_check.config import Config
from cloud_check.features import extract_tile_features_yuv
from cloud_check.pipeline import BURST_SUPPRESS_STAGES
from db import BwFrame, Session


# ── config helpers ────────────────────────────────────────────────────────────

def _parse_time(s: str) -> datetime:
    s = s.strip()
    if s == 'now':
        return datetime.now()
    if s.startswith('-') and s.endswith('h'):
        return datetime.now() - timedelta(hours=float(s[1:-1]))
    if s.startswith('-') and s.endswith('d'):
        return datetime.now() - timedelta(days=float(s[1:-1]))
    return datetime.fromisoformat(s.replace('Z', ''))


def _load_config(path: str) -> dict:
    with open(path) as f:
        raw = f.read()
    # strip _comment keys (not valid JSON, but we allow them as pseudo-comments)
    return json.loads(raw)


# ── comparison helpers ────────────────────────────────────────────────────────

def _compare(check: dict, esp_val: Any, py_val: Any) -> dict | None:
    """Return a mismatch dict or None if values match."""
    ctype = check.get('type', 'exact')
    key = check['esp_key']

    if esp_val is None:
        return None   # ESP didn't emit this key — skip silently

    if ctype == 'exact':
        if str(esp_val) != str(py_val):
            return {'key': key, 'esp_val': esp_val, 'py_val': py_val, 'delta': None}

    elif ctype in ('int', 'float'):
        try:
            ev = float(esp_val)
            pv = float(py_val) if py_val is not None else None
            if pv is None:
                return {'key': key, 'esp_val': esp_val, 'py_val': None, 'delta': None}
            delta = abs(ev - pv)
            tol = float(check.get('tol', 0.5 if ctype == 'float' else 0))
            if delta > tol:
                return {'key': key, 'esp_val': ev, 'py_val': pv,
                        'delta': round(delta, 4)}
        except (TypeError, ValueError):
            if str(esp_val) != str(py_val):
                return {'key': key, 'esp_val': esp_val, 'py_val': py_val, 'delta': None}

    elif ctype == 'bool':
        # ESP stores bool as JSON true/false; Python as Python bool
        ev = bool(esp_val) if not isinstance(esp_val, bool) else esp_val
        pv = bool(py_val) if py_val is not None else None
        if pv is None or ev != pv:
            return {'key': key, 'esp_val': ev, 'py_val': pv, 'delta': None}

    return None


def _get_py_field(result: ClassifierResult, py_field: str, tile_mean_y: np.ndarray) -> Any:
    """Extract a Python-side value by field name.

    'global_mean' is not a ClassifierResult field; compute it from tile_mean_y.
    """
    if py_field == 'global_mean':
        return int(float(tile_mean_y.mean()))  # truncate, matches ESP integer division (gm_sum / CC_NUM_TILES)
    return getattr(result, py_field, None)


def _get_py_field_burst(burst_result, py_field: str, tile_mean_y: np.ndarray) -> Any:
    """Extract a Python-side value for a frame handled entirely by the burst filter.

    Covers suppress stages (DUPLICATE, BRIGHT_STABLE) and skip_bg_model stages (NIGHT).
    Only label, trigger/stage, and global_mean are meaningful; bg-model fields are N/A.
    """
    if py_field == 'label':
        return burst_result.label
    if py_field == 'trigger':
        return burst_result.trigger
    if py_field == 'global_mean':
        return int(float(tile_mean_y.mean()))
    return None   # ratio, warmup, etc. — N/A for burst-only frames


# ── main entry point ──────────────────────────────────────────────────────────

def run(config_path: str | None = None) -> list[dict]:
    """Run parity validation and return per-frame results."""
    if config_path is None:
        config_path = str(_here / 'validate_config.json')

    cfg_raw = _load_config(config_path)
    server_base = cfg_raw.get('server_base', 'http://192.168.1.110:8000').rstrip('/')
    tf = cfg_raw.get('time_frame', {})
    t_from = _parse_time(tf.get('from', '-24h'))
    t_to   = _parse_time(tf.get('to',   'now'))
    checks = [c for c in cfg_raw.get('checks', []) if c.get('esp_key')]

    # Build Python Config from config overrides (defaults match ESP constants).
    pc = cfg_raw.get('python_config', {})
    py_cfg = Config(
        num_photo_buckets         = pc.get('num_photo_buckets',         3),
        num_scene_buckets         = pc.get('num_scene_buckets',         1),
        bright_photo_threshold    = pc.get('bright_photo_threshold',    160),
        lowlight_photo_threshold  = pc.get('lowlight_photo_threshold',  80),
        warmup_frames_per_bucket  = pc.get('warmup_frames_per_bucket',  4),
        ema_alpha                 = pc.get('ema_alpha',                 0.15),
        var_floor                 = pc.get('var_floor',                 36.0),
        init_var                  = pc.get('init_var',                  256.0),
        tile_z_threshold          = pc.get('tile_z_threshold',          3.0),
        quiet_anomaly_ratio       = pc.get('quiet_anomaly_ratio',       0.25),
        dark_object_min_delta     = pc.get('dark_object_min_delta',     20.0),
        dark_object_min_tiles     = pc.get('dark_object_min_tiles',     1),
        dark_blob_max_size        = pc.get('dark_blob_max_size',        5),
        chroma_dark_obj_gate_sq   = pc.get('chroma_dark_obj_gate_sq',   64),
        use_affine_normalization  = pc.get('use_affine_normalization',  False),
        use_chroma_normalization  = pc.get('use_chroma_normalization',  False),
    )

    model = BackgroundModel(py_cfg)
    burst_cfg = BurstConfig(
        night_brightness_threshold = pc.get('night_brightness_threshold', 70.0),
    )
    prev_burst_tile_mean_y: np.ndarray | None = None
    prev_burst_tile_mean_u: np.ndarray | None = None
    prev_burst_tile_mean_v: np.ndarray | None = None
    prev_burst_gm: float | None = None
    prev_burst_ts: datetime | None = None
    results: list[dict] = []
    db = Session()

    try:
        # ── Flash-event anchor ─────────────────────────────────────────────
        # fresh_flash=true is stored in the frame meta itself (no separate table).
        # Find the most recent such frame ≤ t_to and start the Python model from
        # that point so it mirrors the ESP's NVS model reset.
        try:
            flash_frame = (db.query(BwFrame)
                           .filter(BwFrame.captured_at <= t_to,
                                   BwFrame.meta['fresh_flash'].astext == 'true')
                           .order_by(BwFrame.captured_at.desc())
                           .first())
        except Exception:
            flash_frame = None

        if flash_frame and flash_frame.captured_at > t_from:
            effective_from = flash_frame.captured_at
            fw = (flash_frame.meta or {}).get('fw_build', '')
            print(f"[validate] Flash anchor: {flash_frame.captured_at}  fw={fw!r}",
                  file=sys.stderr)
        else:
            effective_from = t_from
            if not flash_frame:
                print("[validate] No flash event in window — model starts from window begin",
                      file=sys.stderr)

        frames = (db.query(BwFrame)
                  .filter(BwFrame.captured_at >= effective_from,
                          BwFrame.captured_at <= t_to,
                          BwFrame.filename.isnot(None))
                  .order_by(BwFrame.captured_at.asc())
                  .all())

        for frame in frames:
            meta = frame.meta or {}

            # ── feature extraction ─────────────────────────────────────────
            # Prefer ESP's own tile_means_y/u/v (identical input → pure logic parity).
            # Fall back to JPEG decode from the server (tests full extraction + logic).
            esp_tile_means_y = meta.get('tile_means')
            esp_tile_means_u = meta.get('tile_means_u')
            esp_tile_means_v = meta.get('tile_means_v')
            expected = py_cfg.grid_h * py_cfg.grid_w
            if (esp_tile_means_y and isinstance(esp_tile_means_y, list)
                    and len(esp_tile_means_y) == expected):
                tile_mean_y = np.array(esp_tile_means_y, dtype=np.float32).reshape(
                    py_cfg.grid_h, py_cfg.grid_w)
                tile_mean_u = (
                    np.array(esp_tile_means_u, dtype=np.float32).reshape(py_cfg.grid_h, py_cfg.grid_w)
                    if esp_tile_means_u and len(esp_tile_means_u) == expected else None
                )
                tile_mean_v = (
                    np.array(esp_tile_means_v, dtype=np.float32).reshape(py_cfg.grid_h, py_cfg.grid_w)
                    if esp_tile_means_v and len(esp_tile_means_v) == expected else None
                )
            else:
                url = f"{server_base}/static/{frame.filename}"
                try:
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    import io
                    from PIL import Image as PILImage
                    ycbcr = PILImage.open(io.BytesIO(resp.content)).convert('YCbCr').resize((640, 480))
                    arr = np.asarray(ycbcr, dtype=np.uint8)
                    feats = extract_tile_features_yuv(arr[:, :, 0], arr[:, :, 1], arr[:, :, 2])
                    tile_mean_y = feats['mean_y']
                    tile_mean_u = feats['mean_u']
                    tile_mean_v = feats['mean_v']
                except Exception as exc:
                    fetch_err = {'key': '_fetch', 'esp_val': None,
                                 'py_val': None, 'delta': str(exc), 'match': False}
                    results.append({
                        'frame_id': frame.id,
                        'captured_at': str(frame.captured_at),
                        'checks': [fetch_err],
                        'mismatches': [fetch_err],
                    })
                    continue

            # ── run burst pre-filter ──────────────────────────────────────
            burst_gm = float(tile_mean_y.mean())
            if prev_burst_ts is not None and frame.captured_at is not None:
                burst_dt = (frame.captured_at.replace(tzinfo=None)
                            - prev_burst_ts.replace(tzinfo=None)).total_seconds()
                if burst_dt < 0:
                    burst_dt = float('inf')
            else:
                burst_dt = float('inf')

            burst_result = burst_classify(
                tile_mean_y, burst_gm, prev_burst_tile_mean_y, prev_burst_gm,
                burst_dt, burst_cfg,
                tile_mean_u=tile_mean_u,
                tile_mean_v=tile_mean_v,
                prev_tile_mean_u=prev_burst_tile_mean_u,
                prev_tile_mean_v=prev_burst_tile_mean_v,
            )
            # Always update burst state (mirrors ESP save_prev — every frame)
            prev_burst_tile_mean_y = tile_mean_y
            prev_burst_tile_mean_u = tile_mean_u
            prev_burst_tile_mean_v = tile_mean_v
            prev_burst_gm = burst_gm
            prev_burst_ts = frame.captured_at

            # Determine how this frame routes through the pipeline.
            # FAST_SHIFT / ISOLATED are dt-dependent; ESP classifies those as
            # BRIGHTNESS_SHIFT → process, so we fall through to bg model here too.
            burst_suppresses = (burst_result.label == 'suppress'
                                and burst_result.trigger in BURST_SUPPRESS_STAGES)

            if burst_suppresses:
                # Burst-suppressed: bg model does not run.
                pass
            elif burst_result.skip_bg_model:
                # NIGHT: upload unconditionally, update model if RTC, skip classifier.
                if meta.get('source') == 'rtc':
                    pb_name = model.photo_bucket_for(burst_gm)
                    pb = photo_bucket_idx(pb_name)
                    sb = model.scene_bucket_for(pb, tile_mean_y)
                    model.observe(pb, sb)
                    model.update(pb, sb, tile_mean_y, tile_mean_u, tile_mean_v)
            else:
                # ── Background model ──────────────────────────────────────
                pb_name = model.photo_bucket_for(burst_gm)
                pb = photo_bucket_idx(pb_name)
                sb = model.scene_bucket_for(pb, tile_mean_y)
                was_warmup = model.warmup_remaining(pb, sb) > 0
                model.observe(pb, sb)
                py_result = classify(
                    tile_mean_y, model, py_cfg,
                    tile_mean_u=tile_mean_u,
                    tile_mean_v=tile_mean_v,
                )

                # Update policy mirrors ESP: only RTC frames update the model.
                # ESP updates on every RTC frame (was_warmup OR QUIET OR AMBIGUOUS/DARK_BLOB).
                if meta.get('source') == 'rtc':
                    model.update(pb, sb, tile_mean_y, tile_mean_u, tile_mean_v)

            # ── compare configured checks ─────────────────────────────────
            checks_detail = []
            mismatches = []
            for check in checks:
                esp_key  = check['esp_key']
                py_field = check['py_field']
                esp_val  = meta.get(esp_key)
                if esp_val is None:
                    continue   # ESP didn't emit this key — skip silently

                if burst_suppresses or burst_result.skip_bg_model:
                    py_val = _get_py_field_burst(burst_result, py_field, tile_mean_y)
                    if py_val is None:
                        continue   # field not meaningful for burst-only frames
                else:
                    py_val = _get_py_field(py_result, py_field, tile_mean_y)

                m = _compare(check, esp_val, py_val)
                if m:
                    m['match'] = False
                    checks_detail.append(m)
                    mismatches.append(m)
                else:
                    checks_detail.append({
                        'key': esp_key, 'esp_val': esp_val, 'py_val': py_val,
                        'delta': None, 'match': True,
                    })

            results.append({
                'frame_id':   frame.id,
                'captured_at': frame.captured_at.strftime('%d.%m.%y %H:%M:%S'),
                'result':     frame.result or '',
                'checks':     checks_detail,
                'mismatches': mismatches,
            })

    finally:
        db.close()

    return results


if __name__ == '__main__':
    args = sys.argv[1:]
    json_mode = '--json' in args
    cfg_args = [a for a in args if a != '--json']
    cfg = cfg_args[0] if cfg_args else None

    out = run(cfg)

    if json_mode:
        # Machine-readable output for Flask subprocess caller
        print(json.dumps(out))
    else:
        total_mm = sum(len(r['mismatches']) for r in out)
        frames_with_mm = sum(1 for r in out if r['mismatches'])
        print(f"Checked {len(out)} frames — {frames_with_mm} with mismatches ({total_mm} total)")
        for r in out:
            if r['mismatches']:
                print(f"  frame {r['frame_id']} ({r['captured_at']}):")
                for m in r['mismatches']:
                    delta = f"  Δ={m['delta']}" if m['delta'] is not None else ''
                    print(f"    {m['key']:20s}  ESP={m['esp_val']}  PY={m['py_val']}{delta}")

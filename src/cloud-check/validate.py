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

from cloud_check.background import BackgroundModel
from cloud_check.classifier import classify, ClassifierResult
from cloud_check.config import Config
from cloud_check.features import extract_tile_features, load_gray_vga
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


def _get_py_field(result: ClassifierResult, py_field: str, tile_mean: np.ndarray) -> Any:
    """Extract a Python-side value by field name.

    'global_mean' is not a ClassifierResult field; compute it from tile_mean.
    """
    if py_field == 'global_mean':
        return int(round(float(tile_mean.mean())))
    return getattr(result, py_field, None)


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
        num_time_buckets        = pc.get('num_time_buckets',         1),
        warmup_frames_per_bucket= pc.get('warmup_frames_per_bucket', 8),
        ema_alpha               = pc.get('ema_alpha',                0.15),
        var_floor               = pc.get('var_floor',                36.0),
        init_var                = pc.get('init_var',                 256.0),
        tile_z_threshold        = pc.get('tile_z_threshold',         2.5),
        quiet_anomaly_ratio     = pc.get('quiet_anomaly_ratio',      0.20),
        dark_object_min_delta   = pc.get('dark_object_min_delta',    35.0),
        dark_object_min_tiles   = pc.get('dark_object_min_tiles',    1),
        temporal_dark_delta     = pc.get('temporal_dark_delta',      20.0),
        scene_drift_min_tiles   = pc.get('scene_drift_min_tiles',    4),
        night_brightness_threshold  = pc.get('night_brightness_threshold', 70.0),
        indirect_light_threshold    = pc.get('indirect_light_threshold',   95.0),
        spot_change_max_tiles       = pc.get('spot_change_max_tiles',      2),
        spot_change_tile_delta      = pc.get('spot_change_tile_delta',     15.0),
        spot_change_global_stability= pc.get('spot_change_global_stability', 10.0),
        spot_change_max_noisy_tiles = pc.get('spot_change_max_noisy_tiles', 20),
    )

    model = BackgroundModel(py_cfg)
    prev_tile_mean: np.ndarray | None = None
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
            hour = frame.captured_at.hour if frame.captured_at else 12

            # ── feature extraction ─────────────────────────────────────────
            # Prefer ESP's own tile_means (identical input → pure logic parity).
            # Fall back to extracting from JPEG (tests extraction + logic).
            esp_tile_means = meta.get('tile_means')
            if esp_tile_means and isinstance(esp_tile_means, list) and len(esp_tile_means) == 192:
                tile_mean = np.array(esp_tile_means, dtype=np.float32).reshape(12, 16)
            else:
                url = f"{server_base}/static/{frame.filename}"
                try:
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    import io
                    from PIL import Image as PILImage
                    img = PILImage.open(io.BytesIO(resp.content))
                    gray = img.convert('L').resize((640, 480))
                    frame_arr = np.asarray(gray, dtype=np.uint8)
                    feats = extract_tile_features(frame_arr)
                    tile_mean = feats['mean']
                except Exception as exc:
                    results.append({
                        'frame_id': frame.id,
                        'captured_at': str(frame.captured_at),
                        'mismatches': [{'key': '_fetch', 'esp_val': None,
                                        'py_val': None, 'delta': str(exc)}],
                    })
                    continue

            # ── run Python classifier ─────────────────────────────────────
            model.observe(hour)
            py_result = classify(tile_mean, hour, model, py_cfg, prev_tile_mean=prev_tile_mean)

            # Update model (mirrors pipeline.py update policy)
            was_warmup = model.warmup_remaining(hour) > 0
            if was_warmup or py_result.label == 'clouds' or py_result.trigger in (
                'SCENE_DRIFT', 'NIGHT', 'INDIRECT_LIGHT'
            ):
                model.update(hour, tile_mean)
            if py_result.trigger == 'SCENE_DRIFT':
                model.reset_warmup(hour)

            prev_tile_mean = tile_mean

            # ── compare configured checks ─────────────────────────────────
            checks_detail = []
            mismatches = []
            for check in checks:
                esp_key  = check['esp_key']
                py_field = check['py_field']
                esp_val  = meta.get(esp_key)
                if esp_val is None:
                    continue   # ESP didn't emit this key — skip
                py_val = _get_py_field(py_result, py_field, tile_mean)
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

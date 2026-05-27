from flask import Flask, request, jsonify, render_template, redirect, send_from_directory, abort
from datetime import datetime, timedelta
from collections import defaultdict
import json
import os
import sys
import requests
import subprocess
import threading
import time
from pathlib import Path
from sqlalchemy.orm.attributes import flag_modified
from db import BwPhoto, BwFrame, Session
from display_spec import DISPLAY_SPEC, DISPLAY_ORDER

# ─── Live background model ────────────────────────────────────────────────────
# Maintains an in-process BackgroundModel that mirrors the ESP's NVS state.
# On startup: replays all stored tile_means from DB (chronological order).
# On each /frame POST: snapshots model means BEFORE update and stores as
# meta['model_tile_means'], enabling the tile overlay Δm display.
_cc_abs = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       '..', 'cloud-check'))
if _cc_abs not in sys.path:
    sys.path.insert(0, _cc_abs)

_LIVE_OK     = False
_live_ready  = False
_live_model  = None
_live_lock   = threading.Lock()
_bg_cfg      = None
_burst_cfg   = None

# Burst filter live state — all guarded by _live_lock
_prev_burst_y:  object = None   # np.ndarray | None
_prev_burst_u:  object = None
_prev_burst_v:  object = None
_prev_burst_gm: object = None   # float | None
_prev_burst_ts: object = None   # datetime | None
_prev_tile_by_cell: dict = {}   # (pb, sb) -> (y_arr, u_arr, v_arr)

_BURST_SUPPRESS_STAGES = frozenset({'DUPLICATE', 'BRIGHT_STABLE'})

try:
    import numpy as _np
    from cloud_check.background import BackgroundModel as _BackgroundModel, photo_bucket_idx as _pb_idx
    from cloud_check.config import Config as _BgConfig
    from cloud_check.burst_filter import BurstConfig as _BurstConfig, burst_classify as _burst_classify
    from cloud_check.classifier import classify as _classify
    from cloud_check.features import load_yuv_vga as _load_yuv_vga, extract_tile_features_yuv as _extract_tile_features_yuv
    _bg_cfg     = _BgConfig(
        num_photo_buckets=3,
        num_scene_buckets=1,
        bright_photo_threshold=160,
        lowlight_photo_threshold=80,
        warmup_frames_per_bucket=4,
    )
    _live_model = _BackgroundModel(_bg_cfg)
    _burst_cfg  = _BurstConfig()
    _LIVE_OK    = True
    print("[live_model] cloud_check loaded OK", flush=True)
except Exception as _import_exc:
    print(f"[live_model] disabled — {_import_exc}", flush=True)


def _should_update(meta: dict) -> bool:
    """Mirror new update policy: RTC frames update on warmup or when result is clouds."""
    return (meta.get('source') == 'rtc'
            and (meta.get('stage') in ('WARMUP', 'NIGHT')  # NIGHT: historical-data compat
                 or meta.get('result') == 'clouds'))


def _warm_live_model():
    """Replay tile_means from DB in chronological order to reconstruct bg model + burst state."""
    global _live_ready, _prev_burst_y, _prev_burst_u, _prev_burst_v
    global _prev_burst_gm, _prev_burst_ts, _prev_tile_by_cell
    if not _LIVE_OK:
        return
    try:
        frames = (Session().query(BwFrame)
                  .filter(BwFrame.filename.isnot(None))
                  .order_by(BwFrame.captured_at.asc())
                  .all())
        n = 0
        grid_size = _bg_cfg.grid_h * _bg_cfg.grid_w
        loc_burst_y = loc_burst_u = loc_burst_v = None
        loc_burst_gm = loc_burst_ts = None
        loc_tile_by_cell: dict = {}

        # Pre-seed the background model from per-bucket corpus averages.
        # Without this the model starts at 128 (flat grey) and needs many RTC
        # frames to converge — LOWLIGHT in particular has very few qualifying
        # frames and ends up stuck far above the real scene mean.
        _LOWLIGHT_SEED_MIN_GM = 60
        _corpus_y: dict[str, list] = {}
        for _f in frames:
            _m = _f.meta or {}
            _pb = _m.get('photo_bucket')
            _tm = _m.get('tile_means')
            _gm_val = _m.get('global_mean', 128)
            if _pb == 'LOWLIGHT' and _gm_val < _LOWLIGHT_SEED_MIN_GM:
                continue
            if _pb and _tm and len(_tm) == grid_size:
                _corpus_y.setdefault(_pb, []).append(
                    _np.array(_tm, dtype=_np.float32).reshape(_bg_cfg.grid_h, _bg_cfg.grid_w)
                )
        for _pb_name, _arrs in _corpus_y.items():
            try:
                _pb_i = _pb_idx(_pb_name)
                _seed = _np.stack(_arrs).mean(axis=0)
                _live_model.seed_from_corpus(_pb_i, 0, _seed)
            except Exception:
                pass

        for frame in frames:
            if not frame.meta:
                continue
            meta = frame.meta
            tile_means_y = meta.get('tile_means')
            if not tile_means_y or len(tile_means_y) != grid_size:
                continue
            arr_y = _np.array(tile_means_y, dtype=_np.float32).reshape(
                        _bg_cfg.grid_h, _bg_cfg.grid_w)
            tm_u = meta.get('tile_means_u')
            arr_u = (_np.array(tm_u, dtype=_np.float32).reshape(_bg_cfg.grid_h, _bg_cfg.grid_w)
                     if tm_u and len(tm_u) == grid_size else None)
            tm_v = meta.get('tile_means_v')
            arr_v = (_np.array(tm_v, dtype=_np.float32).reshape(_bg_cfg.grid_h, _bg_cfg.grid_w)
                     if tm_v and len(tm_v) == grid_size else None)
            gm = meta.get('global_mean', int(arr_y.mean()))
            with _live_lock:
                pb_name = _live_model.photo_bucket_for(gm)
                pb = _pb_idx(pb_name)
                sb = _live_model.scene_bucket_for(pb, arr_y)
                _live_model.observe(pb, sb)
                if _should_update(meta):
                    _live_model.update(pb, sb, arr_y, arr_u, arr_v)

            # Track burst state for all frames
            loc_burst_y  = arr_y
            loc_burst_u  = arr_u
            loc_burst_v  = arr_v
            loc_burst_gm = float(gm)
            loc_burst_ts = frame.captured_at

            # Track prev_tile_by_cell for non-burst-suppressed frames
            burst_trig = meta.get('burst_trigger', '')
            if burst_trig not in _BURST_SUPPRESS_STAGES:
                loc_tile_by_cell[(pb, sb)] = (arr_y, arr_u, arr_v)

            n += 1

        # Publish final burst state atomically
        with _live_lock:
            _prev_burst_y  = loc_burst_y
            _prev_burst_u  = loc_burst_u
            _prev_burst_v  = loc_burst_v
            _prev_burst_gm = loc_burst_gm
            _prev_burst_ts = loc_burst_ts
            _prev_tile_by_cell = loc_tile_by_cell

        _live_ready = True
        print(f"[live_model] warmed up on {n} frames", flush=True)
    except Exception as exc:
        print(f"[live_model] warmup error: {exc}", flush=True)
    finally:
        Session.remove()  # remove thread-local session from scoped_session registry


def _get_tile_features(meta: dict, jpg_path) -> 'tuple':
    """Extract (arr_y, arr_u, arr_v, gm) from ESP meta or by decoding the JPEG.

    Prefers ESP-provided YUV tile means (new firmware, tile_means_u present).
    Falls back to JPEG decode so old-firmware frames still get server-side analysis.
    Returns (None, None, None, None) on failure.
    """
    gsz = _bg_cfg.grid_h * _bg_cfg.grid_w
    esp_y = meta.get('tile_means')
    esp_u = meta.get('tile_means_u')
    esp_v = meta.get('tile_means_v')

    if (esp_y and isinstance(esp_y, list) and len(esp_y) == gsz
            and esp_u and isinstance(esp_u, list) and len(esp_u) == gsz):
        arr_y = _np.array(esp_y, dtype=_np.float32).reshape(_bg_cfg.grid_h, _bg_cfg.grid_w)
        arr_u = _np.array(esp_u, dtype=_np.float32).reshape(_bg_cfg.grid_h, _bg_cfg.grid_w)
        arr_v = (_np.array(esp_v, dtype=_np.float32).reshape(_bg_cfg.grid_h, _bg_cfg.grid_w)
                 if esp_v and isinstance(esp_v, list) and len(esp_v) == gsz else None)
        gm = int(float(arr_y.mean()))
        return arr_y, arr_u, arr_v, gm

    from pathlib import Path as _Path
    p = _Path(jpg_path) if not hasattr(jpg_path, 'exists') else jpg_path
    if p.exists():
        try:
            y_arr, u_arr, v_arr = _load_yuv_vga(p)
            feats = _extract_tile_features_yuv(y_arr, u_arr, v_arr)
            return feats['mean_y'], feats['mean_u'], feats['mean_v'], feats['global_mean']
        except Exception:
            pass
    return None, None, None, None

# ─────────────────────────────────────────────────────────────────────────────

birdwatch_http = "http://192.168.1.43"
global_status = "PIR_Sensor"
session = Session  # scoped_session proxy — each thread gets its own Session instance
threading.Thread(target=_warm_live_model, daemon=True).start()

app = Flask(__name__, static_folder=os.getenv('JPG_FOLDER_PATH'), static_url_path='/static')

@app.teardown_appcontext
def shutdown_session(exc):
    Session.remove()  # return the thread-local session to the pool after each request
print(os.getenv('JPG_FOLDER_PATH'))


@app.route('/legacy')
def index():
    # Pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = 100  # Number of entries per page
    
    # Calculate offset
    offset = (page - 1) * per_page
    
    # Get total count of entries
    total_entries = session.query(BwPhoto).count()
    total_pages = (total_entries + per_page - 1) // per_page  # Ceiling division
    
    # Get entries for current page
    entries = session.query(BwPhoto).order_by(BwPhoto.date.desc()).offset(offset).limit(per_page).all()
    
    latest_image_entry = session.query(BwPhoto).filter(BwPhoto.filename.isnot(None)).order_by(BwPhoto.id.desc()).first()

    last_seen = None
    last_seen_detail = None
    if latest_image_entry and latest_image_entry.date:
        s = int((datetime.now() - latest_image_entry.date).total_seconds())
        if s < 60:
            last_seen = f"{s}s ago"
        elif s < 3600:
            last_seen = f"{s // 60}m {s % 60}s ago"
        elif s < 86400:
            last_seen = f"{s // 3600}h {(s % 3600) // 60}m ago"
        else:
            last_seen = f"{s // 86400}d ago"
        last_seen_detail = latest_image_entry.date.strftime("%d.%m.%y %H:%M:%S")

    return render_template('index.html',
                           status=global_status,
                           entries=entries,
                           page=page,
                           total_pages=total_pages,
                           last_seen=last_seen,
                           last_seen_detail=last_seen_detail)


# called from the web page
@app.route('/set_status')
def set_status():
    global global_status
    global_status = request.args.get('status', 'PIR_Sensor')
    print('** SET Global status:', global_status)
    if global_status == "PIR_Sensor":
        try:
            requests.get(birdwatch_http+'/stop',timeout=1)
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
    return redirect('/')

@app.route('/status', methods=['POST'])
def status():
    print('Status request received')
    data = request.data.decode('utf-8')
    print(data)
    battery = request.json.get('battery')
    source = request.json.get('source')
    trigger = request.json.get('trigger')
    date = datetime.now()

    try:
        battery = float(battery)
    except (TypeError, ValueError):
        print("Invalid battery value")
        return jsonify({'message': 'Invalid battery value'}), 400

    new_photo = BwPhoto(source=source, date=date, voltage=battery, debug=trigger)
    session.add(new_photo)
    session.commit()

    return jsonify({'status': 'OK'}), 200

def handle_large_request(e):
    print("Request entity too large")
    return jsonify({'message': 'File too large'}), 413



@app.route('/upload', methods=['POST'])


def process_request_upload_file():
    global global_status
    print("Upload request received")
    try:
        print(f"Request content length: {request.content_length}")
        print(f"Request Image length: {request.headers['Image-Length']}")
        image_length = int(request.headers['Image-Length'])

        image = request.files.get('image')
        if not image:
            print("No file part in the request")
            return jsonify({'message': 'No file part in the request'}), 400

        print(f"Image filename: {image.filename}")
        filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '.jpg'
        file_path = os.path.join(os.getenv('JPG_FOLDER_PATH', '/tmp'), filename)

        with open(file_path, 'wb') as f:
            print(f"Opened file: {file_path}")
            chunk_size = 2048
            total_size = 0
            wait_count = 0
            content_length = request.content_length
            while True:
                chunk = image.stream.read(chunk_size)
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
                    print(f"Chunk length: {len(chunk)} - Total size: {total_size}")
                else:
                    if content_length and total_size >= image_length:
                        print("All data received.")
                        break
                    else:
                        wait_count += 1
                        time.sleep(0.5)
                        print("Waiting for more data...")
                        if wait_count > 20:
                            print("Timeout waiting for more data")
                            return jsonify({'message': 'Timeout waiting for more data'}), 408
                        continue

            print(f"Total size written: {total_size} bytes")

        print('File uploaded successfully')

        battery = request.form.get('battery')
        source = request.form.get('source')
        trigger = request.form.get('trigger')
        cc_label   = request.form.get('cc_label')
        cc_stage   = request.form.get('cc_stage')
        photo_mode = request.form.get('photo_mode')

        print(f"Form data - battery: {battery}, source: {source}, trigger: {trigger}, cc: {cc_label}/{cc_stage}, photo_mode: {photo_mode}")

        date = datetime.now()
        try:
            battery = float(battery)
        except (TypeError, ValueError):
            print("Invalid battery value")
            return jsonify({'message': 'Invalid battery value'}), 400

        new_photo = BwPhoto(source=source, date=date, voltage=battery, debug=trigger, filename=filename,
                            cc_label=cc_label, cc_stage=cc_stage, photo_mode=photo_mode)
        session.add(new_photo)
        session.commit()

        print('Global status:', global_status)

        return jsonify({
            'message': "Battery voltage: " + str(battery),
            'file_message': 'File uploaded successfully',
            'global_status': global_status
        }), 200
    except Exception as e:
        print(f"Exception occurred: {e}")
        return jsonify({'message': 'Server error'}), 500

@app.route('/browse_results')
def browse_results():
    IMAGE_FOLDER = os.getenv('JPG_FOLDER_PATH')
    images = sorted(
        [f for f in os.listdir(IMAGE_FOLDER) if f.endswith('.jpg')],
        key=lambda x: os.path.getmtime(os.path.join(IMAGE_FOLDER, x)),
        reverse=True
    )
    if not images:
        return "No images available", 404

    # Check if an ID was provided in the request
    entry_id = request.args.get('id')
    if entry_id:
        # Find the entry with the given ID
        entry = session.query(BwPhoto).filter(BwPhoto.id == entry_id).first()
        if entry and entry.filename:
            # Find the index of this image in our sorted list
            try:
                current_index = images.index(entry.filename)
            except ValueError:
                # If the image is not found in the list, default to index 0
                current_index = 0
        else:
            # If entry not found or has no filename, default to index 0
            current_index = 0
    else:
        # If no ID provided, use the index parameter as before
        current_index = int(request.args.get('index', 0))
    
    # Ensure the index is within bounds
    current_index = max(0, min(current_index, len(images) - 1))

    current_image = images[current_index]
    image_path = os.path.join(IMAGE_FOLDER, current_image)
    timestamp = datetime.fromtimestamp(os.path.getmtime(image_path)).strftime('%Y-%m-%d %H:%M:%S')

    next_index = (current_index + 1) % len(images)
    prev_index = (current_index - 1 + len(images)) % len(images)

    current_entry = session.query(BwPhoto).filter(BwPhoto.filename == current_image).first()

    time_diff = None
    if current_entry and current_index < len(images) - 1:
        prev_image = images[current_index + 1]  # images are sorted newest-first
        prev_entry = session.query(BwPhoto).filter(BwPhoto.filename == prev_image).first()
        if prev_entry and current_entry.date and prev_entry.date:
            time_diff_seconds = (current_entry.date - prev_entry.date).total_seconds()
            if time_diff_seconds < 60:
                time_diff = f"{int(time_diff_seconds)} seconds"
            elif time_diff_seconds < 3600:
                time_diff = f"{int(time_diff_seconds / 60)} minutes {int(time_diff_seconds % 60)} seconds"
            else:
                hours = int(time_diff_seconds / 3600)
                minutes = int((time_diff_seconds % 3600) / 60)
                time_diff = f"{hours} hours {minutes} minutes"

    return render_template(
        'browse_results.html',
        image_path=current_image,
        next_index=next_index,
        prev_index=prev_index,
        current_index=current_index,
        total_images=len(images),
        time_diff=time_diff,
        entry=current_entry,
    )


@app.route('/battery')
def battery():
    volt_rows = (session.query(BwPhoto.date, BwPhoto.voltage)
                 .filter(BwPhoto.voltage.isnot(None), BwPhoto.voltage > 0)
                 .order_by(BwPhoto.date.asc())
                 .all())
    photo_rows = (session.query(BwPhoto.date, BwPhoto.cc_label)
                  .filter(BwPhoto.filename.isnot(None))
                  .order_by(BwPhoto.date.asc())
                  .all())

    # Group by 3-hour windows: 00:00, 03:00, 06:00, 09:00, etc.
    volt_by_3h = defaultdict(list)
    for r in volt_rows:
        if r.date:
            # Round down to nearest 3-hour boundary
            h3 = (r.date.hour // 3) * 3
            bucket_time = r.date.replace(hour=h3, minute=0, second=0, microsecond=0)
            volt_by_3h[bucket_time].append(float(r.voltage))

    cloud_by_3h   = defaultdict(int)
    process_by_3h = defaultdict(int)
    for r in photo_rows:
        if r.date:
            h3 = (r.date.hour // 3) * 3
            bucket_time = r.date.replace(hour=h3, minute=0, second=0, microsecond=0)
            if r.cc_label == 'clouds':
                cloud_by_3h[bucket_time] += 1
            else:
                process_by_3h[bucket_time] += 1

    hourly = []
    if volt_by_3h:
        start_3h = min(volt_by_3h.keys())
        end_3h   = datetime.now().replace(minute=0, second=0, microsecond=0)
        # Round end to nearest 3-hour boundary
        h3 = (end_3h.hour // 3) * 3
        end_3h = end_3h.replace(hour=h3)

        last_v  = None
        cur = start_3h
        while cur <= end_3h:
            if cur in volt_by_3h:
                v = round(sum(volt_by_3h[cur]) / len(volt_by_3h[cur]), 3)
                last_v = v
                est = False
            elif last_v is not None:
                v = last_v
                est = True
            else:
                cur += timedelta(hours=3)
                continue
            hourly.append({'t': cur.strftime('%Y-%m-%dT%H:%M:%S'), 'v': v, 'est': est,
                           'nc': cloud_by_3h.get(cur, 0),
                           'np': process_by_3h.get(cur, 0)})
            cur += timedelta(hours=3)

    daily = defaultdict(list)
    for r in volt_rows:
        if r.date and r.voltage:
            daily[r.date.strftime('%Y-%m-%d')].append(float(r.voltage))

    daily_rows = [
        {'day': day, 'count': len(vs),
         'min': round(min(vs), 2), 'avg': round(sum(vs) / len(vs), 2), 'max': round(max(vs), 2)}
        for day, vs in sorted(daily.items(), reverse=True)
    ]

    return render_template('battery.html', hourly=hourly, daily_rows=daily_rows)


# ─────────────────────────────── /frame (new telemetry upload) ───────────────

@app.route('/frame', methods=['POST'])
def process_frame_upload():
    """Generic upload endpoint for the new schema-less telemetry pipeline.

    Accepts multipart/form-data with:
        meta  — JSON string produced by bw_tele_json() on the ESP
        image — JPEG binary

    All telemetry keys land in bw_frames.meta (JSONB).  Only 'result' and
    'captured_at' are promoted to columns for fast filtering.
    """
    global global_status
    global _prev_burst_y, _prev_burst_u, _prev_burst_v, _prev_burst_gm, _prev_burst_ts
    try:
        image = request.files.get('image')
        if not image:
            return jsonify({'message': 'No image in request'}), 400

        filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '.jpg'
        file_path = os.path.join(os.getenv('JPG_FOLDER_PATH', '/tmp'), filename)

        with open(file_path, 'wb') as f:
            image_length = int(request.headers.get('Image-Length', 0))
            content_length = request.content_length
            chunk_size = 2048
            total_size = 0
            wait_count = 0
            while True:
                chunk = image.stream.read(chunk_size)
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
                else:
                    if image_length and total_size >= image_length:
                        break
                    elif not image_length and total_size > 0:
                        break
                    else:
                        wait_count += 1
                        time.sleep(0.5)
                        if wait_count > 20:
                            return jsonify({'message': 'Timeout waiting for image data'}), 408

        meta_raw = request.form.get('meta', '{}')
        try:
            meta = json.loads(meta_raw)
        except (json.JSONDecodeError, ValueError):
            meta = {}

        # Snapshot raw ESP payload before any server-side modification
        esp_meta = dict(meta)

        captured_at_str = meta.get('captured_at')
        if captured_at_str:
            try:
                captured_at = datetime.fromisoformat(captured_at_str.replace('Z', '+00:00'))
            except ValueError:
                captured_at = datetime.now()
        else:
            captured_at = datetime.now()

        # ── Server-side pipeline (shadow mode) ───────────────────────────────
        # Re-run burst + background-model classification using the live Python model.
        # Overwrites algorithm fields in meta; preserves manual/external fields.
        # Raw ESP values are preserved under meta['esp_meta'] for comparison.
        if _LIVE_OK and _live_ready:
            arr_y, arr_u, arr_v, gm = _get_tile_features(meta, file_path)
            if arr_y is not None:
                try:
                    with _live_lock:
                        # dt since previous burst frame
                        dt = float('inf')
                        if _prev_burst_ts is not None and captured_at is not None:
                            dt = max(0.0, (captured_at - _prev_burst_ts).total_seconds())

                        burst = _burst_classify(
                            arr_y, float(gm),
                            _prev_burst_y, _prev_burst_gm,
                            dt, _burst_cfg,
                            tile_mean_u=arr_u, tile_mean_v=arr_v,
                            prev_tile_mean_u=_prev_burst_u, prev_tile_mean_v=_prev_burst_v,
                        )

                        pb_name = _live_model.photo_bucket_for(gm)
                        pb = _pb_idx(pb_name)
                        sb = _live_model.scene_bucket_for(pb, arr_y)
                        prev_cell = _prev_tile_by_cell.get((pb, sb))
                        was_warmup = _live_model.warmup_remaining(pb, sb) > 0
                        _live_model.observe(pb, sb)

                        # Snapshot model BEFORE update (drives tile overlay Δm display)
                        snap_y = _live_model.mean_y[pb, sb].flatten().round().astype(int).tolist()
                        snap_u = _live_model.mean_u[pb, sb].flatten().round().astype(int).tolist()
                        snap_v = _live_model.mean_v[pb, sb].flatten().round().astype(int).tolist()

                        source = meta.get('source')
                        burst_suppresses = (burst.label == 'suppress'
                                            and burst.trigger in _BURST_SUPPRESS_STAGES)
                        if burst_suppresses:
                            srv_result = 'clouds'
                            srv_stage  = burst.trigger
                            bg_pred    = None
                        elif burst.skip_bg_model:
                            # NIGHT: upload unconditionally, update model if RTC, skip bg model.
                            srv_result = 'process'
                            srv_stage  = burst.trigger
                            bg_pred    = None
                            if source == 'rtc':
                                _live_model.update(pb, sb, arr_y, arr_u, arr_v)
                        else:
                            bg_pred = _classify(
                                arr_y, _live_model, _bg_cfg,
                                tile_mean_u=arr_u, tile_mean_v=arr_v,
                            )
                            srv_result = bg_pred.label
                            srv_stage  = bg_pred.trigger

                            if source == 'rtc' and (was_warmup or bg_pred.label == 'clouds'):
                                _live_model.update(pb, sb, arr_y, arr_u, arr_v)
                            _prev_tile_by_cell[(pb, sb)] = (arr_y, arr_u, arr_v)

                        # Advance burst state for next frame
                        _prev_burst_y  = arr_y
                        _prev_burst_u  = arr_u
                        _prev_burst_v  = arr_v
                        _prev_burst_gm = float(gm)
                        _prev_burst_ts = captured_at

                    # Build server-computed fields
                    new_fields: dict = {
                        'tile_means':         arr_y.flatten().round().astype(int).tolist(),
                        'model_tile_means':   snap_y,
                        'model_tile_means_u': snap_u,
                        'model_tile_means_v': snap_v,
                        'global_mean':        int(gm),
                        'photo_bucket':       pb_name,
                        'result':             srv_result,
                        'stage':              srv_stage,
                        'warmup':             bool(was_warmup),
                        'prev_valid':         bool(prev_cell is not None),
                        'burst_trigger':      burst.trigger,
                        'burst_label':        burst.label,
                        'burst_gm_diff':      round(burst.gm_diff, 1),
                        'burst_n_changed':    int(burst.n_changed),
                        'burst_n_dark':       int(burst.n_dark),
                        'burst_n_chroma':     int(burst.n_chroma_changed),
                        'simulated':          True,
                    }
                    if arr_u is not None:
                        new_fields['tile_means_u'] = arr_u.flatten().round().astype(int).tolist()
                    if arr_v is not None:
                        new_fields['tile_means_v'] = arr_v.flatten().round().astype(int).tolist()
                    if bg_pred is not None:
                        color = _np.zeros(bg_pred.dark_tile_mask.shape, dtype=_np.int8)
                        color[bg_pred.dark_tile_mask] = 1
                        color[bg_pred.dark_blob_mask] = 2
                        new_fields.update({
                            'ratio':            round(float(bg_pred.anomaly_ratio), 3),
                            'dark_anomalous':   int(bg_pred.anomaly_mask.sum()),
                            'dark_tiles':       int(bg_pred.dark_tiles),
                            'dark_blob_max':    int(bg_pred.dark_blob_max),
                            'scene_bucket':     int(bg_pred.scene_bucket),
                            'n_chroma_changed': int(bg_pred.n_chroma_changed),
                            'tile_delta_luma':  bg_pred.tile_delta_luma.flatten().round().astype(int).tolist(),
                            'tile_color_mask':  color.flatten().tolist(),
                        })
                        if bg_pred.tile_delta_chroma is not None:
                            new_fields['tile_delta_chroma'] = bg_pred.tile_delta_chroma.flatten().round(1).tolist()
                    else:
                        new_fields.update({
                            'ratio': 0.0,
                            'dark_anomalous': 0, 'dark_tiles': 0, 'dark_blob_max': 0,
                        })

                    # Merge: server values overwrite ESP values; preserve manual keys
                    meta = {**meta, **new_fields}
                    print(f"[sim] {filename} burst={burst.trigger} bg={srv_stage} result={srv_result}",
                          flush=True)
                except Exception as _pipe_exc:
                    print(f"[sim] pipeline error for {filename}: {_pipe_exc}", flush=True)

        meta['esp_meta'] = esp_meta   # always store raw ESP snapshot
        result = meta.get('result')

        frame = BwFrame(
            captured_at=captured_at,
            result=result,
            filename=filename,
            meta=meta,
        )
        session.add(frame)

        session.commit()

        return jsonify({
            'message': 'frame uploaded',
            'global_status': global_status,
        }), 200

    except Exception as e:
        print(f"Exception in /frame: {e}")
        return jsonify({'message': 'Server error'}), 500


# ─────────────────────────────── /frames gallery ─────────────────────────────

@app.route('/')
@app.route('/frames')
def frames_index():
    page = request.args.get('page', 1, type=int)
    per_page = 100
    offset = (page - 1) * per_page

    # ── Filters ──────────────────────────────────────────────────────────────
    # Source checkboxes: ?src=pir,rtc  (defaults: both on)
    src_param  = request.args.get('src', 'pir,rtc')
    src_active = set(src_param.split(',')) if src_param else set()
    show_pir   = 'pir' in src_active
    show_rtc   = 'rtc' in src_active

    # Label checkboxes: ?lbl=bird,ignore,special,cloud,none  (defaults: all on)
    lbl_param    = request.args.get('lbl', 'bird,ignore,special,cloud,none')
    lbl_active   = set(lbl_param.split(',')) if lbl_param else set()
    show_bird    = 'bird'    in lbl_active
    show_deleted = 'ignore'  in lbl_active
    show_special = 'special' in lbl_active
    show_cloud   = 'cloud'   in lbl_active   # result='clouds'
    show_none    = 'none'    in lbl_active   # unlabeled process frames

    from sqlalchemy import or_, and_, false as sql_false, true as sql_true

    q = session.query(BwFrame)

    # Source filter (PIR frames may have source missing/null — treat those as pir)
    if not (show_pir and show_rtc):
        if not show_pir and not show_rtc:
            q = q.filter(sql_false())
        elif show_pir and not show_rtc:
            q = q.filter(or_(
                BwFrame.meta['source'].astext == 'pir',
                BwFrame.meta['source'].astext.is_(None),
            ))
        else:  # rtc only
            q = q.filter(BwFrame.meta['source'].astext == 'rtc')

    # Label / result filter
    all_lbl = show_bird and show_deleted and show_special and show_cloud and show_none
    if not all_lbl:
        lbl_clauses = []
        if show_cloud:
            lbl_clauses.append(BwFrame.result == 'clouds')
        # Process frames filtered by label
        process_clauses = []
        if show_bird:
            process_clauses.append(BwFrame.meta['label'].astext == 'bird')
        if show_deleted:
            process_clauses.append(BwFrame.meta['label'].astext == 'ignore')
        if show_special:
            process_clauses.append(BwFrame.meta['label'].astext == 'special')
        if show_none:
            process_clauses.append(and_(
                BwFrame.meta['label'].astext.is_(None),
                BwFrame.result != 'clouds',
            ))
        if process_clauses:
            lbl_clauses.append(or_(*process_clauses))
        if lbl_clauses:
            q = q.filter(or_(*lbl_clauses))
        else:
            q = q.filter(sql_false())

    total      = q.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page       = min(page, total_pages)
    entries    = q.order_by(BwFrame.captured_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    latest = (session.query(BwFrame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.desc()).first())
    last_seen = last_seen_detail = None
    if latest and latest.captured_at:
        s = int((datetime.now() - latest.captured_at).total_seconds())
        if s < 60:    last_seen = f"{s}s ago"
        elif s < 3600: last_seen = f"{s // 60}m {s % 60}s ago"
        elif s < 86400: last_seen = f"{s // 3600}h {(s % 3600) // 60}m ago"
        else:          last_seen = f"{s // 86400}d ago"
        last_seen_detail = latest.captured_at.strftime("%d.%m.%y %H:%M:%S")

    return render_template('frames.html',
                           status=global_status,
                           entries=entries,
                           page=page,
                           total_pages=total_pages,
                           last_seen=last_seen,
                           last_seen_detail=last_seen_detail,
                           spec=DISPLAY_SPEC,
                           order=DISPLAY_ORDER,
                           src_active=src_active,
                           lbl_active=lbl_active,
                           src_param=src_param,
                           lbl_param=lbl_param)


@app.route('/frame_detail')
def frame_detail():
    from sqlalchemy import or_, and_, false as sql_false

    entry_id = request.args.get('id', type=int)
    if entry_id is None:
        return redirect('/frames')

    entry = session.query(BwFrame).filter(BwFrame.id == entry_id).first()
    if not entry:
        return "Not found", 404

    # Respect gallery filter params so prev/next stays within the filtered set
    src_param = request.args.get('src', 'pir,rtc')
    lbl_param = request.args.get('lbl', 'bird,ignore,special,cloud,none')
    src_active = set(src_param.split(',')) if src_param else {'pir', 'rtc'}
    lbl_active = set(lbl_param.split(',')) if lbl_param else {'bird', 'ignore', 'special', 'cloud', 'none'}

    def _apply_filters(q):
        show_pir = 'pir' in src_active
        show_rtc = 'rtc' in src_active
        if not (show_pir and show_rtc):
            if not show_pir and not show_rtc:
                q = q.filter(sql_false())
            elif show_pir:
                q = q.filter(or_(BwFrame.meta['source'].astext == 'pir',
                                 BwFrame.meta['source'].astext.is_(None)))
            else:
                q = q.filter(BwFrame.meta['source'].astext == 'rtc')

        all_lbl = lbl_active >= {'bird', 'ignore', 'special', 'cloud', 'none'}
        if not all_lbl:
            clauses = []
            if 'cloud' in lbl_active:
                clauses.append(BwFrame.result == 'clouds')
            proc = []
            if 'bird'    in lbl_active: proc.append(BwFrame.meta['label'].astext == 'bird')
            if 'ignore'  in lbl_active: proc.append(BwFrame.meta['label'].astext == 'ignore')
            if 'special' in lbl_active: proc.append(BwFrame.meta['label'].astext == 'special')
            if 'none'    in lbl_active: proc.append(and_(BwFrame.meta['label'].astext.is_(None), BwFrame.result != 'clouds'))
            if proc:
                clauses.append(or_(*proc))
            q = q.filter(or_(*clauses)) if clauses else q.filter(sql_false())
        return q

    prev_q = _apply_filters(session.query(BwFrame).filter(BwFrame.id < entry_id))
    next_q = _apply_filters(session.query(BwFrame).filter(BwFrame.id > entry_id))
    prev_entry = prev_q.order_by(BwFrame.id.desc()).first()
    next_entry = next_q.order_by(BwFrame.id.asc()).first()

    # Filter suffix appended to prev/next nav URLs — always included so
    # state is self-contained in the URL and survives copy-paste/refresh.
    filter_qs = f'&src={src_param}&lbl={lbl_param}'

    time_diff = None
    if prev_entry and entry.captured_at and prev_entry.captured_at:
        s = (entry.captured_at - prev_entry.captured_at).total_seconds()
        if s < 60:    time_diff = f"{int(s)}s"
        elif s < 3600: time_diff = f"{int(s / 60)}m {int(s % 60)}s"
        else:          time_diff = f"{int(s / 3600)}h {int((s % 3600) / 60)}m"

    prev_tile_means = None
    if prev_entry and prev_entry.meta:
        prev_tile_means = prev_entry.meta.get('tile_means')

    model_tile_means = entry.meta.get('model_tile_means') if entry.meta else None

    return render_template('frame_detail.html',
                           entry=entry,
                           prev_id=prev_entry.id if prev_entry else None,
                           next_id=next_entry.id if next_entry else None,
                           filter_qs=filter_qs,
                           time_diff=time_diff,
                           spec=DISPLAY_SPEC,
                           order=DISPLAY_ORDER,
                           prev_tile_means=prev_tile_means,
                           model_tile_means=model_tile_means)


# ─────────────────────────────── /validate ───────────────────────────────────

_validate_results: list = []   # last run results held in-process (dev/admin use)
_validate_running: bool = False

_backfill_log: str = ''
_backfill_running: bool = False


# ─────────────────────────────── /frame/<id>/label ───────────────────────────

_VALID_LABELS = {'bird', 'ignore', 'special'}


@app.route('/frame/<int:frame_id>/label', methods=['POST'])
def set_label(frame_id: int):
    data = request.get_json(silent=True) or {}
    label = data.get('label')
    if label is not None and label not in _VALID_LABELS:
        return jsonify({'error': f'invalid label; use one of {sorted(_VALID_LABELS)}'}), 400

    frame = session.query(BwFrame).filter(BwFrame.id == frame_id).first()
    if not frame:
        return jsonify({'error': 'not found'}), 404

    meta = dict(frame.meta or {})
    if label is None:
        meta.pop('label', None)
    else:
        meta['label'] = label
    frame.meta = meta
    flag_modified(frame, 'meta')
    session.commit()
    return jsonify({'id': frame_id, 'label': label}), 200


# ─────────────────────────────── /admin/export ───────────────────────────────

_EXPORT_LABELS = {'bird', 'ignore', 'special', 'all'}


@app.route('/admin/export/<label>')
def export_list(label: str):
    """Return JSON list of labeled frames for the download script.

    GET /admin/export/bird           → frames with label=bird, not yet downloaded
    GET /admin/export/all            → all labeled frames, not yet downloaded
    GET /admin/export/bird?include_downloaded=true  → include already-downloaded
    """
    if label not in _EXPORT_LABELS:
        return jsonify({'error': f'unknown label; use one of {sorted(_EXPORT_LABELS)}'}), 400

    include_downloaded = request.args.get('include_downloaded', 'false').lower() == 'true'

    q = session.query(BwFrame).filter(
        BwFrame.filename.isnot(None),
        BwFrame.meta['label'].astext.isnot(None),
    )
    if label != 'all':
        q = q.filter(BwFrame.meta['label'].astext == label)
    if not include_downloaded:
        q = q.filter(BwFrame.meta['downloaded_at'].is_(None))

    frames = q.order_by(BwFrame.captured_at.asc()).all()

    result = [
        {
            'id': f.id,
            'filename': f.filename,
            'captured_at': f.captured_at.isoformat() if f.captured_at else None,
            'label': (f.meta or {}).get('label'),
        }
        for f in frames
    ]
    return jsonify({'label': label, 'count': len(result), 'frames': result})


@app.route('/admin/image/<int:frame_id>')
def admin_image(frame_id: int):
    """Serve a frame's JPG by id (mirrors /static/<filename> without needing the name).

    Used by remote/dev tooling to inspect arbitrary frames without first
    looking up the filename via /admin/export/all.
    """
    frame = session.query(BwFrame).filter(BwFrame.id == frame_id).first()
    if not frame or not frame.filename:
        abort(404)
    folder = os.getenv('JPG_FOLDER_PATH', '/tmp')
    if not os.path.isabs(folder):
        folder = os.path.abspath(folder)
    return send_from_directory(folder, frame.filename, mimetype='image/jpeg')


@app.route('/admin/meta/<int:frame_id>')
def admin_meta(frame_id: int):
    """Return the full meta JSONB + captured_at + result for a frame by id."""
    frame = session.query(BwFrame).filter(BwFrame.id == frame_id).first()
    if not frame:
        abort(404)
    return jsonify({
        'id': frame.id,
        'captured_at': frame.captured_at.isoformat() if frame.captured_at else None,
        'result': frame.result,
        'filename': frame.filename,
        'meta': frame.meta or {},
    })


@app.route('/frame/<int:frame_id>/downloaded', methods=['POST'])
def mark_downloaded(frame_id: int):
    """Mark a frame as downloaded (sets meta['downloaded_at'])."""
    frame = session.query(BwFrame).filter(BwFrame.id == frame_id).first()
    if not frame:
        return jsonify({'error': 'not found'}), 404
    meta = dict(frame.meta or {})
    meta['downloaded_at'] = datetime.now().isoformat()
    frame.meta = meta
    flag_modified(frame, 'meta')
    session.commit()
    return jsonify({'id': frame_id, 'downloaded_at': meta['downloaded_at']}), 200


@app.route('/admin/backfill', methods=['GET'])
def backfill_status():
    return jsonify({'running': _backfill_running, 'log': _backfill_log})


@app.route('/admin/backfill', methods=['POST'])
def backfill_run():
    global _backfill_log, _backfill_running
    if _backfill_running:
        return jsonify({'status': 'already running'}), 409

    force = (request.json or {}).get('force', False)

    def _run():
        global _backfill_log, _backfill_running
        _backfill_running = True
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            cc_dir = os.path.join(here, '..', 'cloud-check')
            venv_py = os.path.join(cc_dir, '.venv', 'bin', 'python')
            if not os.path.exists(venv_py):
                venv_py = 'python3'
            script = os.path.join(cc_dir, 'backfill_meta.py')
            args = [venv_py, script]
            if force:
                args.append('--force')
            proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
            _backfill_log = proc.stdout
            if proc.stderr:
                _backfill_log += '\n--- stderr ---\n' + proc.stderr
            if proc.returncode != 0:
                _backfill_log = f'ERROR (exit {proc.returncode}):\n' + _backfill_log
        except Exception as exc:
            _backfill_log = f'Exception: {exc}'
        finally:
            _backfill_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'started'}), 202


@app.route('/validate/run', methods=['POST'])
def validate_run():
    """Run validate.py via its own venv (cloud-check has numpy/Pillow/scipy).

    validate.py is called as a subprocess; results are returned as JSON on stdout.
    Runs in a background thread so the HTTP response returns immediately.
    """
    global _validate_results, _validate_running
    if _validate_running:
        return jsonify({'status': 'already running'}), 409

    def _run():
        global _validate_results, _validate_running
        _validate_running = True
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            cc_dir = os.path.join(here, '..', 'cloud-check')
            # Use the cloud-check venv python which has numpy/Pillow/scipy
            venv_py = os.path.join(cc_dir, '.venv', 'bin', 'python')
            if not os.path.exists(venv_py):
                venv_py = 'python3'   # fallback: hope numpy is available
            validate_script = os.path.join(cc_dir, 'validate.py')
            config_path = os.path.join(cc_dir, 'validate_config.json')
            proc = subprocess.run(
                [venv_py, validate_script, config_path, '--json'],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                _validate_results = [{'error': proc.stderr.strip() or 'validate.py exited with error'}]
            else:
                _validate_results = json.loads(proc.stdout)
        except Exception as exc:
            _validate_results = [{'error': str(exc)}]
        finally:
            _validate_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'started'}), 202


@app.route('/validate')
def validate_view():
    return render_template('validate.html',
                           results=_validate_results,
                           running=_validate_running)


if __name__ == '__main__':
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.run(host='0.0.0.0', port=8000, threaded=True)

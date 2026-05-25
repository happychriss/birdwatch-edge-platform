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
_UPDATE_STAGES = {'NIGHT', 'WARMUP', 'QUIET', 'SCENE_DRIFT'}

try:
    import numpy as _np
    from cloud_check.background import BackgroundModel as _BackgroundModel
    from cloud_check.config import Config as _BgConfig
    _bg_cfg     = _BgConfig(num_time_buckets=1, warmup_frames_per_bucket=0)
    _live_model = _BackgroundModel(_bg_cfg)
    _LIVE_OK    = True
    print("[live_model] cloud_check loaded OK", flush=True)
except Exception as _import_exc:
    print(f"[live_model] disabled — {_import_exc}", flush=True)


def _warm_live_model():
    """Replay tile_means from DB in chronological order to reconstruct bg model state."""
    global _live_ready
    if not _LIVE_OK:
        return
    try:
        frames = (Session().query(BwFrame)
                  .filter(BwFrame.filename.isnot(None))
                  .order_by(BwFrame.captured_at.asc())
                  .all())
        n = 0
        grid_size = _bg_cfg.grid_h * _bg_cfg.grid_w
        for frame in frames:
            if not frame.meta:
                continue
            tile_means = frame.meta.get('tile_means')
            if not tile_means or len(tile_means) != grid_size:
                continue
            stage = frame.meta.get('stage', '')
            hour  = frame.captured_at.hour if frame.captured_at else 12
            arr   = _np.array(tile_means, dtype=_np.float32).reshape(
                        _bg_cfg.grid_h, _bg_cfg.grid_w)
            with _live_lock:
                _live_model.observe(hour)
                if stage in _UPDATE_STAGES:
                    _live_model.update(hour, arr)
                if stage == 'SCENE_DRIFT':
                    _live_model.reset_warmup(hour)
            n += 1
        _live_ready = True
        print(f"[live_model] warmed up on {n} frames", flush=True)
    except Exception as exc:
        print(f"[live_model] warmup error: {exc}", flush=True)
    finally:
        Session.remove()  # remove thread-local session from scoped_session registry

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

    volt_by_hour = defaultdict(list)
    for r in volt_rows:
        if r.date:
            volt_by_hour[r.date.replace(minute=0, second=0, microsecond=0)].append(float(r.voltage))

    cloud_by_hour   = defaultdict(int)
    process_by_hour = defaultdict(int)
    for r in photo_rows:
        if r.date:
            hk = r.date.replace(minute=0, second=0, microsecond=0)
            if r.cc_label == 'clouds':
                cloud_by_hour[hk] += 1
            else:
                process_by_hour[hk] += 1

    hourly = []
    if volt_by_hour:
        start_h = min(volt_by_hour.keys())
        end_h   = datetime.now().replace(minute=0, second=0, microsecond=0)
        last_v  = None
        cur = start_h
        while cur <= end_h:
            if cur in volt_by_hour:
                v = round(sum(volt_by_hour[cur]) / len(volt_by_hour[cur]), 3)
                last_v = v
                est = False
            elif last_v is not None:
                v = last_v
                est = True
            else:
                cur += timedelta(hours=1)
                continue
            hourly.append({'t': cur.strftime('%Y-%m-%dT%H:%M:%S'), 'v': v, 'est': est,
                           'nc': cloud_by_hour.get(cur, 0),
                           'np': process_by_hour.get(cur, 0)})
            cur += timedelta(hours=1)

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

        captured_at_str = meta.get('captured_at')
        if captured_at_str:
            try:
                captured_at = datetime.fromisoformat(captured_at_str.replace('Z', '+00:00'))
            except ValueError:
                captured_at = datetime.now()
        else:
            captured_at = datetime.now()

        result = meta.get('result')

        # Inject server-computed model_tile_means when ESP hasn't sent it yet.
        # Once the ESP firmware is updated to emit model_tile_means, this branch
        # is skipped ('model_tile_means' already in meta).
        if _LIVE_OK and _live_ready and 'model_tile_means' not in meta:
            _tm = meta.get('tile_means')
            if _tm and len(_tm) == _bg_cfg.grid_h * _bg_cfg.grid_w:
                _arr  = _np.array(_tm, dtype=_np.float32).reshape(
                            _bg_cfg.grid_h, _bg_cfg.grid_w)
                _hour = captured_at.hour
                _stg  = meta.get('stage', '')
                with _live_lock:
                    _live_model.observe(_hour)
                    _bkt  = _live_model._idx(_hour)
                    # Snapshot BEFORE update — mirrors what z-scores were computed from
                    _snap = _live_model.mean[_bkt].flatten().round().astype(int).tolist()
                    if _stg in _UPDATE_STAGES:
                        _live_model.update(_hour, _arr)
                    if _stg == 'SCENE_DRIFT':
                        _live_model.reset_warmup(_hour)
                meta['model_tile_means'] = _snap

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

    total = session.query(BwFrame).count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    entries = (session.query(BwFrame)
               .order_by(BwFrame.captured_at.desc())
               .offset(offset).limit(per_page).all())

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
                           order=DISPLAY_ORDER)


@app.route('/frame_detail')
def frame_detail():
    entry_id = request.args.get('id', type=int)
    if entry_id is None:
        return redirect('/frames')

    entry = session.query(BwFrame).filter(BwFrame.id == entry_id).first()
    if not entry:
        return "Not found", 404

    prev_entry = (session.query(BwFrame)
                  .filter(BwFrame.id < entry_id)
                  .order_by(BwFrame.id.desc()).first())
    next_entry = (session.query(BwFrame)
                  .filter(BwFrame.id > entry_id)
                  .order_by(BwFrame.id.asc()).first())

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

# This is a sample Python script.

# Press Shift+F10 to execute it or replace it with your code.
# Press Double Shift to search everywhere for classes, files, tool windows, actions, and settings.
from flask import Flask, request, jsonify, render_template, redirect
from datetime import datetime, timedelta
from collections import defaultdict
import os
import requests
import time
from db import BwPhoto, Session, init_schema


birdwatch_http = "http://192.168.1.43"
global_status = "PIR_Sensor"
session = Session()

app = Flask(__name__, static_folder=os.getenv('JPG_FOLDER_PATH'), static_url_path='/static')
print(os.getenv('JPG_FOLDER_PATH'))


@app.route('/')
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


if __name__ == '__main__':
    init_schema()
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.run(host='0.0.0.0', port=8000)

# See PyCharm help at https://www.jetbrains.com/help/pycharm/

# This is a sample Python script.

# Press Shift+F10 to execute it or replace it with your code.
# Press Double Shift to search everywhere for classes, files, tool windows, actions, and settings.
from flask import Flask, request, jsonify, render_template, redirect
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import os
import re
import requests
import time
import threading
from collections import deque
load_dotenv()

_ansi_re = re.compile(r'\x1b\[[0-9;]*m')

# ─── Remote log buffer (filled by POST /log from the device) ─────────────────
_log_lines = deque(maxlen=500)
_log_lock  = threading.Lock()

# Set maximum allowed payload to 16MB (adjust as needed)


# get username and password from environment variables
username = os.getenv('DB_USERNAME')
password = os.getenv('DB_PASSWORD')
birdwatch_http="http://192.168.1.43"
global_status = "PIR_Sensor"
engine = create_engine(f'postgresql://{username}:{password}@192.168.1.110:5432/goodwatch')
# Step 4: Define the Battery class
Base = declarative_base()

class Battery(Base):
    __tablename__ = 'birdwatch_battery'

    id = Column(Integer, primary_key=True)
    source = Column(String)
    date = Column(DateTime)
    voltage = Column(Float)
    pinstatus = Column(String)
    trigger = Column(String)
    filename = Column(String)
    brightdiff = Column(Float)
    ldr_power = Column(Float)
# Step 5: Create a session
Session = sessionmaker(bind=engine)
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
    total_entries = session.query(Battery).count()
    total_pages = (total_entries + per_page - 1) // per_page  # Ceiling division
    
    # Get entries for current page
    entries = session.query(Battery).order_by(Battery.date.desc()).offset(offset).limit(per_page).all()
    
    latest_image_entry = session.query(Battery).filter(Battery.filename.isnot(None)).order_by(Battery.id.desc()).first()
    image_path = None
    if latest_image_entry:
        image_path = latest_image_entry.filename
    
    return render_template('index.html', 
                          status=global_status, 
                          entries=entries, 
                          image_path=image_path,
                          page=page,
                          total_pages=total_pages)


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
    brightdiff = request.json.get('brightdiff')  # Read brightdiff
    ldr_power = request.json.get('ldr_power')  # Read ldr_power
    date = datetime.now()

    try:
        battery = float(battery)
        brightdiff = float(brightdiff)  # Convert brightdiff to float
        ldr_power = float(ldr_power) if ldr_power is not None else None  # Convert ldr_power to float if it exists
    except (TypeError, ValueError):
        print("Invalid battery, brightdiff, or ldr_power value")
        return jsonify({'message': 'Invalid battery, brightdiff, or ldr_power value'}), 400

    new_battery = Battery(source=source, date=date, voltage=battery, trigger=trigger, brightdiff=brightdiff, ldr_power=ldr_power)
    session.add(new_battery)
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
        brightdiff = request.form.get('brightdiff')  # Read brightdiff
        ldr_power = request.form.get('ldr_power')  # Read ldr_power

        print(f"Form data - battery: {battery}, source: {source}, trigger: {trigger}, brightdiff: {brightdiff}, ldr_power: {ldr_power}")

        date = datetime.now()
        try:
            battery = float(battery)
            brightdiff = float(brightdiff)  # Convert brightdiff to float
            ldr_power = float(ldr_power) if ldr_power is not None else None  # Convert ldr_power to float if it exists
        except (TypeError, ValueError):
            print("Invalid battery, brightdiff, or ldr_power value")
            return jsonify({'message': 'Invalid battery, brightdiff, or ldr_power value'}), 400

        new_battery = Battery(source=source, date=date, voltage=battery, trigger=trigger, filename=filename, brightdiff=brightdiff, ldr_power=ldr_power)
        session.add(new_battery)
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
        entry = session.query(Battery).filter(Battery.id == entry_id).first()
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

    # Get the entry from the database for the current image
    current_entry = session.query(Battery).filter(Battery.filename == current_image).first()
    
    # Initialize variables
    ldr_power = None
    time_diff = None
    
    if current_entry:
        ldr_power = current_entry.ldr_power
        
        # If there's a previous image, calculate the time difference
        if current_index < len(images) - 1:
            prev_image = images[current_index + 1]  # Since images are sorted in reverse order
            prev_entry = session.query(Battery).filter(Battery.filename == prev_image).first()
            
            if prev_entry and current_entry.date and prev_entry.date:
                # Calculate time difference in seconds
                time_diff_seconds = (current_entry.date - prev_entry.date).total_seconds()
                
                # Format time difference
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
        timestamp=timestamp,
        image_path=current_image,
        next_index=next_index,
        prev_index=prev_index,
        ldr_power=ldr_power,
        time_diff=time_diff
    )


@app.route('/log', methods=['POST'])
def receive_log():
    lines = request.get_json(silent=True) or []
    ts = datetime.now().strftime('%H:%M:%S')
    with _log_lock:
        for line in lines:
            _log_lines.append(f"{ts}  {_ansi_re.sub('', line)}")
    return '', 204

@app.route('/logs')
def logs():
    with _log_lock:
        lines = list(_log_lines)
    return render_template('logs.html', lines=lines)

@app.route('/logs/data')
def logs_data():
    with _log_lock:
        lines = list(_log_lines)
    return jsonify(lines)

@app.route('/logs/clear', methods=['POST'])
def logs_clear():
    with _log_lock:
        _log_lines.clear()
    return '', 204

if __name__ == '__main__':
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.run(host='0.0.0.0', port=8000)

# See PyCharm help at https://www.jetbrains.com/help/pycharm/

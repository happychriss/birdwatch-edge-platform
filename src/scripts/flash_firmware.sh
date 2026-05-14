#!/usr/bin/env bash
# Build and flash the BirdWatch ESP32-S3 firmware.
#
# Usage: ./flash_firmware.sh [port]
#   port  defaults to /dev/ttyACM0
#
# Run from inside the container:
#   /workspace/src/scripts/flash_firmware.sh
#   /workspace/src/scripts/flash_firmware.sh /dev/ttyACM1

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-/dev/ttyACM0}"
PROJECT_DIR="$SCRIPT_DIR/../esp_bw_src"

if [ ! -f "$PROJECT_DIR/CMakeLists.txt" ]; then
    echo "ERROR: $PROJECT_DIR/CMakeLists.txt not found"
    exit 1
fi

echo "=== building + flashing → $PORT ==="
source /home/ubuntu/esp-idf/export.sh > /dev/null 2>&1
cd "$PROJECT_DIR"
idf.py -p "$PORT" build flash
echo "=== done — run monitor.sh to see serial output ==="

#!/usr/bin/env bash
# Start the BirdWatch Flask server (port 8000).
#
# Usage: ./start_server.sh
# Run from inside the container:
#   /workspace/src/scripts/start_server.sh
# Run via Docker exec:
#   docker exec -it <container_id> /workspace/src/scripts/start_server.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/../python_bw_src"

if lsof -ti:8000 > /dev/null 2>&1; then
    echo "=== stopping existing server on port 8000 ==="
    kill "$(lsof -ti:8000)" 2>/dev/null || true
    sleep 1
fi

echo "=== starting BirdWatch server at $SERVER_DIR ==="
cd "$SERVER_DIR"
mkdir -p jpg_folder
exec .venv/bin/python main.py

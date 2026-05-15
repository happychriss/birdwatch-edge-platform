#!/bin/bash
set -e
cd "$(dirname "$0")/../cloud-check"

echo "=== cloud-check pipeline ==="
.venv/bin/python -m scripts.evaluate --skip-aux

# Kill any existing server on 8001 (lsof not available, use ss)
SERVER_PID=$(ss -tlnp sport = :8001 2>/dev/null | grep -oP 'pid=\K[0-9]+' || true)
if [ -n "$SERVER_PID" ]; then
    echo ""
    echo "=== restarting server (pid $SERVER_PID) ==="
    kill "$SERVER_PID" 2>/dev/null || true
    sleep 1
else
    echo ""
    echo "=== starting server ==="
fi

echo "Gallery — refresh browser for latest results: http://localhost:8001/gallery"
.venv/bin/python serve.py --port 8001

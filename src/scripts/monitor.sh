#!/usr/bin/env bash
# Monitor ESP32 serial output via pyserial (idf_monitor needs interactive TTY).
# Resets the board on start so output is captured from the beginning.
#
# Usage: ./monitor.sh [port]
#   port  defaults to /dev/ttyACM0

PORT="${1:-/dev/ttyACM0}"

echo "=== monitoring $PORT — resetting board (Ctrl+C to stop) ==="
python3 - "$PORT" <<'EOF'
import serial, sys, time

port = sys.argv[1]
s = serial.Serial(port, 115200, timeout=0.1)

s.dtr = False
s.rts = True
time.sleep(0.1)
s.rts = False
time.sleep(0.1)
print("--- board reset ---", flush=True)

try:
    while True:
        data = s.read(256)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
except KeyboardInterrupt:
    pass
finally:
    s.close()
EOF

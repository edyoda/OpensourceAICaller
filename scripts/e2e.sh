#!/usr/bin/env bash
# Boot the server on a scratch port, run the headless loopback, tear it down.
#   ./scripts/e2e.sh           -> one full turn
#   ./scripts/e2e.sh bargein   -> talk over the agent mid-reply
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
MODE=${1:-normal}
PORT=${PORT:-8078}
LOG=$(mktemp)

$PY -m uvicorn app.main:app --port "$PORT" --log-level info >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

$PY - "$PORT" <<'EOF'
import socket, sys, time
for _ in range(150):
    try:
        socket.create_connection(("localhost", int(sys.argv[1])), 0.2).close(); sys.exit(0)
    except OSError: time.sleep(0.1)
sys.exit("server never came up")
EOF

PORT="$PORT" $PY scripts/loopback_test.py "$MODE"

echo "--- server log ---"
grep -Ei "barge-in|stt tail|llm first|tts first|error|exception" "$LOG" || echo "(clean)"

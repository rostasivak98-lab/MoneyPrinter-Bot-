#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

mkdir -p data

# --- env ---
export PO_SSID="${PO_SSID:-$(cat po_ssid.txt 2>/dev/null || true)}"
export PO_IS_DEMO="${PO_IS_DEMO:-1}"
export PO_SYMBOL="${PO_SYMBOL:-CHFJPY_otc}"
export FEED_BASE="${FEED_BASE:-http://127.0.0.1:8001}"

kill_port () {
  local PORT="$1"
  local PIDS
  PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$PIDS" ]; then
    echo "Killing port $PORT pids: $PIDS"
    kill -TERM $PIDS 2>/dev/null || true
    sleep 0.5
    PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    [ -z "$PIDS" ] || kill -KILL $PIDS 2>/dev/null || true
  fi
}

echo "Stopping old processes..."
kill_port 8001
kill_port 8000
pkill -f "csv_recorder.py" 2>/dev/null || true
sleep 0.2

echo "Starting FEED 8001..."
nohup uvicorn app.main:app --host 0.0.0.0 --port 8001 > data/feed_8001.log 2>&1 &

for i in {1..60}; do
  if curl -sS "$FEED_BASE/openapi.json" >/dev/null 2>&1; then
    echo "FEED is up"
    break
  fi
  sleep 0.25
done

if ! curl -sS "$FEED_BASE/openapi.json" >/dev/null 2>&1; then
  echo "FEED failed to start. Tail:"
  tail -n 120 data/feed_8001.log || true
  exit 1
fi

echo "Engine start requested..."
curl -sS -X POST "$FEED_BASE/api/start" >/dev/null 2>&1 || true

echo "Waiting for first candles..."
ok=0
for i in {1..120}; do
  ST="$(curl -sS "$FEED_BASE/api/status" 2>/dev/null || true)"
  if [ -n "$ST" ] && echo "$ST" | grep -q '"candles":\[' && ! echo "$ST" | grep -q '"candles":\[\]'; then
    ok=1
    break
  fi
  sleep 0.5
done

if [ "$ok" -ne 1 ]; then
  echo "Candles still empty. Debug:"
  echo "Last status:"
  curl -sS "$FEED_BASE/api/status" || true
  echo
  echo "Tail feed log:"
  tail -n 120 data/feed_8001.log || true
  exit 1
fi

echo "Candles OK"

echo "Starting CSV recorder..."
nohup python3 csv_recorder.py > data/csv_recorder.log 2>&1 &
sleep 0.3

CNT="$(ps aux | grep -F "csv_recorder.py" | grep -v grep | wc -l | tr -d ' ')"
PIDS="$(ps aux | grep -F "csv_recorder.py" | grep -v grep | awk '{print $2}' | tr '\n' ' ')"

if [ "$CNT" != "1" ]; then
  echo "Recorder count=$CNT (expected 1). PIDs: $PIDS"
  echo "Fix: pkill -f csv_recorder.py ; ./run_all.sh"
  tail -n 80 data/csv_recorder.log || true
  exit 1
fi

echo "Recorder PID: $PIDS"
echo "Quick status:"
curl -sS "$FEED_BASE/api/status"; echo

echo "Done."

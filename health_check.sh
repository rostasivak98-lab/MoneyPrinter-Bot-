#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "FEED port 8001:"
lsof -nP -iTCP:8001 -sTCP:LISTEN || true
echo

echo "Recorder process:"
pgrep -fl "python3 csv_recorder.py" || true
echo

echo "API status:"
curl -sS http://127.0.0.1:8001/api/status; echo
echo

CSV="data/candles_CHFJPY_otc.csv"
echo "CSV file:"
ls -lah "$CSV" || true
echo

echo "Last CSV line:"
tail -n 1 "$CSV" || true
echo

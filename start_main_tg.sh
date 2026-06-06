#!/usr/bin/env bash
set -euo pipefail

cd ~/bot-srdce
source .venv/bin/activate
source .env.telegram

python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000

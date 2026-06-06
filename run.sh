#!/bin/zsh
test -f .env.local && source .env.local
set -e

cd /Users/macbook/bot-srdce
source .venv/bin/activate
export PO_SSID="$(cat po_ssid.txt)"
export PO_IS_DEMO="1"

(sleep 1; open "http://127.0.0.1:8000/") &

exec uvicorn app.main:app --reload

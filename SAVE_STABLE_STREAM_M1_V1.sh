#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== 1) .gitignore (safe) ==="
cat > .gitignore <<'GIT'
.venv/
__pycache__/
*.pyc
.DS_Store
po_ssid.txt
*.log
/tmp/
playwright/.cache/
data/*.csv
candles*.csv
*.bak_*
*_OLD_*.csv
GIT

echo "=== 2) git init + commit + tag ==="
git init >/dev/null 2>&1 || true
git add -A
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git commit -m "FREEZE: STABLE_STREAM_M1_V1 (STREAM age_sec=0, NORMAL+HA match, seeded HA)" >/dev/null
  echo "Committed."
fi

git tag -f STABLE_STREAM_M1_V1 >/dev/null

echo "=== 3) snapshot folder copy ==="
cd ..
rm -rf bot-srdce__STABLE_STREAM_M1_V1 2>/dev/null || true
cp -a bot-srdce bot-srdce__STABLE_STREAM_M1_V1

echo "=== 4) zip snapshot (without venv + ssid) ==="
rm -f bot-srdce__STABLE_STREAM_M1_V1.zip 2>/dev/null || true
zip -rq bot-srdce__STABLE_STREAM_M1_V1.zip bot-srdce__STABLE_STREAM_M1_V1 \
  -x "*/.venv/*" "*/po_ssid.txt"

echo "=== DONE ✅ ==="
echo
echo "RETURN (git):"
echo "  cd ~/bot-srdce && git checkout -f STABLE_STREAM_M1_V1"
echo
echo "RETURN (snapshot):"
echo "  rm -rf ~/bot-srdce && cp -a ~/bot-srdce__STABLE_STREAM_M1_V1 ~/bot-srdce"
echo
echo "FILES CREATED:"
echo "  ~/bot-srdce__STABLE_STREAM_M1_V1"
echo "  ~/bot-srdce__STABLE_STREAM_M1_V1.zip"
echo
echo "Tag:"
cd bot-srdce
git rev-parse --short HEAD || true
git tag | tail -n 20 || true

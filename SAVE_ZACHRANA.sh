#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== GIT COMMIT (ZACHRANA) ==="
git add -A
if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git commit -m "FREEZE: ZACHRANA (current stable working state)" >/dev/null
  echo "Committed."
fi

git tag -f ZACHRANA >/dev/null

echo "=== SNAPSHOT COPY ==="
cd ..
rm -rf bot-srdce__ZACHRANA 2>/dev/null || true
cp -a bot-srdce bot-srdce__ZACHRANA

echo "=== ZIP SNAPSHOT ==="
rm -f bot-srdce__ZACHRANA.zip 2>/dev/null || true
zip -rq bot-srdce__ZACHRANA.zip bot-srdce__ZACHRANA \
  -x "*/.venv/*" "*/po_ssid.txt"

echo
echo "=== HOTOVO ZACHRANA ✅ ==="
echo
echo "NÁVRAT GIT:"
echo "cd ~/bot-srdce && git checkout -f ZACHRANA"
echo
echo "NÁVRAT SNAPSHOT:"
echo "rm -rf ~/bot-srdce && cp -a ~/bot-srdce__ZACHRANA ~/bot-srdce"
echo
echo "VYTVOŘENO:"
echo "~/bot-srdce__ZACHRANA"
echo "~/bot-srdce__ZACHRANA.zip"
echo
git rev-parse --short HEAD
git tag | tail -n 20

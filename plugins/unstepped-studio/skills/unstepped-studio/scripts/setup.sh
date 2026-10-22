#!/bin/bash
# Installs Unstepped Studio on this Mac. The app itself comes from your company's Unstepped Cloud: during the 7-day
# trial, or after signing in. Run again to repair. Never touches ~/unstepped-review/home/ (account, styles, settings).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/unstepped-review"
SERVER="$(tr -d ' \n' < "$DIR/server.txt")"
case "$SERVER" in *REPLACE-ME*) echo "This installer has no server address yet (scripts/server.txt)."; exit 1;; esac
mkdir -p "$DEST/home"
[ -f "$DEST/cloud.py" ] || { cp "$DIR/cloud.py" "$DEST/cloud.py"; echo 0.0.0 > "$DEST/VERSION"; }
/usr/bin/python3 "$DEST/cloud.py" server "$SERVER" >/dev/null || true
if ! /usr/bin/python3 "$DEST/cloud.py" update --force; then
  echo "To get the app, sign in first:  /usr/bin/python3 ~/unstepped-review/cloud.py signin <email> <code from your admin>"
  echo "then run this again."
  exit 1
fi
echo
echo "Checking the tools it needs:"
for t in ffmpeg node; do command -v $t >/dev/null && echo "  ok       $t" || echo "  MISSING  $t  (brew install $t; Home > Settings can install it)"; done
echo
echo "Installed $(cat "$DEST/VERSION"). Start Home:  /usr/bin/python3 ~/unstepped-review/home.py serve"
echo "Then sign in on Home > Settings > Account with your email and the code from your admin."

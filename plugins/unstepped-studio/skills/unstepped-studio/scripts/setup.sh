#!/bin/bash
# Installs Unstepped Studio on this Mac. Nothing of the app is downloaded until the person signs in: a new Mac gets
# a sign-in page first (in the browser), then the app installs, Home is set to start at login, and it opens.
# Run again to repair. Never touches ~/unstepped-review/home/ (account, styles, settings).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/unstepped-review"
PY=/usr/bin/python3
SERVER="$(tr -d ' \n' < "$DIR/server.txt")"
case "$SERVER" in *REPLACE-ME*) echo "This installer has no server address yet (scripts/server.txt)."; exit 1;; esac
mkdir -p "$DEST/home"
[ -f "$DEST/cloud.py" ] || cp "$DIR/cloud.py" "$DEST/cloud.py"
[ -f "$DEST/VERSION" ] || echo 0.0.0 > "$DEST/VERSION"
$PY "$DEST/cloud.py" server "$SERVER" >/dev/null 2>&1 || true
if $PY "$DEST/cloud.py" status | grep -q '"state": "signed-in"'; then
  $PY "$DEST/cloud.py" update >/dev/null 2>&1 || true
  URL="$($PY "$DEST/cloud.py" start)"
  echo "Unstepped Studio $(cat "$DEST/VERSION") is installed and signed in. Home: $URL"
  exit 0
fi
pkill -f "unstepped-review/cloud.py gate" 2>/dev/null || true
nohup $PY "$DEST/cloud.py" gate > "$DEST/home/gate.log" 2>&1 &
for i in $(seq 1 20); do grep -q "^http" "$DEST/home/gate.log" 2>/dev/null && break; sleep 0.25; done
echo "A sign-in page opened in your browser: $(head -1 "$DEST/home/gate.log")"
echo "Sign in there with your work email and the code from your admin; Unstepped Studio installs and opens by itself."

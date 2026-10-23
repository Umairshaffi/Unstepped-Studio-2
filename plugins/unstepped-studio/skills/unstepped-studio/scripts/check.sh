#!/bin/bash
# Claude session start: check in with Unstepped Cloud (a logout from the admin, app updates).
# Quiet unless something needs doing. Its output reaches the session.
APP="$HOME/unstepped-review/cloud.py"
if [ -f "$APP" ]; then
  /usr/bin/python3 "$APP" check --quiet 2>/dev/null
else
  echo "Unstepped Studio is installed as a plugin but not set up on this Mac. To set it up: bash \"$(cd "$(dirname "$0")" && pwd)/setup.sh\""
fi
exit 0

#!/bin/bash
# Maintainer only.
#   ./release.sh app 2.1.0 "What changed"   publish the app from ~/unstepped-review to Unstepped Cloud. Every signed-in
#                                           Mac installs it on its next check-in. Nothing goes to GitHub.
#   ./release.sh installer 2.0.1            the installer plugin changed (scripts, SKILL.md, hooks): bump its version,
#                                           validate and commit; then `git push`.
set -euo pipefail
R="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
  app)
    V="${2:?version, like 2.1.0}"
    cp "$HOME/unstepped-review/cloud.py" "$R/plugins/unstepped-studio/skills/unstepped-studio/scripts/cloud.py"
    /usr/bin/python3 "$HOME/unstepped-review/cloud.py" publish "$V" "${3:-}"
    if ! git -C "$R" diff --quiet; then echo "Note: the installer's copy of cloud.py changed: run ./release.sh installer <version> too."; fi ;;
  installer)
    V="${2:?version, like 2.0.1}"
    /usr/bin/python3 - "$R" "$V" <<'PY'
import json, sys
from pathlib import Path
r, v = Path(sys.argv[1]), sys.argv[2]
for f in (r / ".claude-plugin/marketplace.json", r / "plugins/unstepped-studio/.claude-plugin/plugin.json"):
    d = json.loads(f.read_text())
    if "plugins" in d:
        d["plugins"][0]["version"] = v
    else:
        d["version"] = v
    f.write_text(json.dumps(d, indent=2) + "\n")
PY
    claude plugin validate "$R" >/dev/null
    git -C "$R" add -A && git -C "$R" commit -q -m "Installer $V" && echo "Committed installer $V. Push it with: git -C \"$R\" push" ;;
  *) sed -n '2,7p' "$0"; exit 1 ;;
esac

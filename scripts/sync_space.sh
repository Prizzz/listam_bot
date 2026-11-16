#!/usr/bin/env bash
# Copy the bot into a cloned Hugging Face Space repo (keeps the Space's own README.md front matter).
# Usage: scripts/sync_space.sh /path/to/cloned-space
set -euo pipefail
SPACE="${1:?usage: sync_space.sh /path/to/cloned-space}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -d "$SPACE/.git" ] || { echo "not a git clone: $SPACE" >&2; exit 1; }
rm -rf "$SPACE/listam"
cp -R "$ROOT/listam" "$SPACE/listam"
find "$SPACE/listam" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$ROOT/space/app.py" "$ROOT/space/requirements.txt" "$ROOT/space/packages.txt" "$SPACE/"
echo "Synced. Review and publish:"
echo "  cd $SPACE && git add -A && git commit -m 'Update bot' && git push"

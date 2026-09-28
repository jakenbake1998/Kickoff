#!/bin/bash
# Run by the Kickoff window for one folder: fetch the latest engine from GitHub (if online), then
# run it with progress events for the window. Offline or on any error, the installed engine is kept.
SUPPORT="$HOME/Library/Application Support/Kickoff"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PY="$SUPPORT/venv/bin/python3"
BASE="$(cat "$SUPPORT/update-url.txt" 2>/dev/null)"
if [ -n "$BASE" ]; then
  if curl -fsSL --max-time 15 "$BASE/musicsync.py" -o "$SUPPORT/musicsync.py.run" 2>/dev/null \
     && "$PY" -m py_compile "$SUPPORT/musicsync.py.run" 2>/dev/null \
     && ! cmp -s "$SUPPORT/musicsync.py.run" "$SUPPORT/musicsync.py"; then
    mv "$SUPPORT/musicsync.py.run" "$SUPPORT/musicsync.py"
    echo "Engine updated to $("$PY" "$SUPPORT/musicsync.py" --version)." >&2
  fi
  rm -f "$SUPPORT/musicsync.py.run"
fi
command -v ffmpeg >/dev/null || { echo "error: ffmpeg isn't installed. Run the Kickoff installer again." >&2; exit 1; }
exec "$PY" "$SUPPORT/musicsync.py" --events "$1"

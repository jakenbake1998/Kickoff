#!/bin/bash
# Runs Kickoff on one folder. Before each run it fetches the latest engine from GitHub (if online),
# so fixes arrive without reinstalling. Offline or on any error, it keeps the installed engine.
SUPPORT="$HOME/Library/Application Support/Kickoff"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PY="$SUPPORT/venv/bin/python3"
BASE="$(cat "$SUPPORT/update-url.txt" 2>/dev/null)"
if [ -n "$BASE" ]; then
  if curl -fsSL --max-time 15 "$BASE/musicsync.py" -o "$SUPPORT/musicsync.py.new" 2>/dev/null \
     && "$PY" -m py_compile "$SUPPORT/musicsync.py.new" 2>/dev/null; then
    if ! cmp -s "$SUPPORT/musicsync.py.new" "$SUPPORT/musicsync.py"; then
      mv "$SUPPORT/musicsync.py.new" "$SUPPORT/musicsync.py"
      echo "Kickoff updated to $("$PY" "$SUPPORT/musicsync.py" --version)."
    fi
  fi
  rm -f "$SUPPORT/musicsync.py.new"
fi
clear
"$PY" "$SUPPORT/musicsync.py" "$1" && open "$1/Kickoff Exports" \
  && echo && echo "Done. In Premiere: File > Import the .xml in Kickoff Exports." \
  || echo "Kickoff hit an error; see above."

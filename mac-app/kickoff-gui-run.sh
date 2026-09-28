#!/bin/bash
# Run by the Kickoff window: fetch the latest engine from GitHub (if online), then run it with
# progress events for the window. Arguments: -- MODE (auto, music or setup) rebuild|add PATH...
# (the folders and song in the window's list); an older window passes FOLDER MODE rebuild|add.
# Offline or on any error, the installed engine is kept.
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
if [ "$1" = "--" ]; then
  MODE="$2"; HOW="$3"; shift 3
else
  MODE="$2"; HOW="$3"; set -- "$1"
fi
EXTRA=()
[ "$HOW" = "rebuild" ] && EXTRA=(--rebuild)
# where the XML goes, from the window: a folder, or "drive" for the top of the footage's drive
if [ -n "$KICKOFF_XML_DIR" ] && grep -q -- '--xml-dir' "$SUPPORT/musicsync.py"; then
  EXTRA+=(--xml-dir "$KICKOFF_XML_DIR")
fi
# what to call the project XML, from the window ("" or unset: the footage folder's name)
[ -n "$KICKOFF_NAME" ] && EXTRA+=(--name "$KICKOFF_NAME")
exec "$PY" "$SUPPORT/musicsync.py" --events --mode "${MODE:-auto}" "${EXTRA[@]}" -- "$@"

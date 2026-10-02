#!/bin/bash
# Run by the Kickoff window at launch, in the background: fetch the newest engine, window page and
# app source from GitHub. The engine applies right away, the window from the next launch. When the
# app source changed it is recompiled; the new app is swapped in only if it builds.
# Prints one line for the window's footer when something was updated.
SUPPORT="$HOME/Library/Application Support/Kickoff"
APP="$1"
PY="$SUPPORT/venv/bin/python3"
BASE="$(cat "$SUPPORT/update-url.txt" 2>/dev/null)"
[ -n "$BASE" ] || exit 0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
get() { curl -fsSL --max-time 20 "$BASE/$1" -o "$TMP/$(basename "$1")" 2>/dev/null; }
changed=""

if get musicsync.py && "$PY" -m py_compile "$TMP/musicsync.py" 2>/dev/null \
   && ! cmp -s "$TMP/musicsync.py" "$SUPPORT/musicsync.py"; then
  mv "$TMP/musicsync.py" "$SUPPORT/musicsync.py.upd" && mv "$SUPPORT/musicsync.py.upd" "$SUPPORT/musicsync.py"
  changed="engine $("$PY" "$SUPPORT/musicsync.py" --version 2>/dev/null)"
fi
for f in kickoff-gui-run.sh kickoff-update.sh; do
  if get "mac-app/$f" && head -1 "$TMP/$f" | grep -q '^#!/bin/bash' && ! cmp -s "$TMP/$f" "$SUPPORT/$f"; then
    cp "$TMP/$f" "$SUPPORT/$f.upd" && mv "$SUPPORT/$f.upd" "$SUPPORT/$f"
  fi
done
if get mac-app/ui/index.html && grep -q "window.Kickoff" "$TMP/index.html" \
   && ! cmp -s "$TMP/index.html" "$SUPPORT/ui/index.html"; then
  cp "$TMP/index.html" "$SUPPORT/ui/index.html.upd" && mv "$SUPPORT/ui/index.html.upd" "$SUPPORT/ui/index.html"
  changed="${changed:+$changed, }window"
fi
# the empty Premiere project that "Open in Premiere" copies into the chosen folder (a gzip file)
if get mac-app/Blank.prproj && [ "$(head -c 2 "$TMP/Blank.prproj" | od -An -tx1 | tr -d ' ')" = "1f8b" ] \
   && ! cmp -s "$TMP/Blank.prproj" "$SUPPORT/Blank.prproj"; then
  cp "$TMP/Blank.prproj" "$SUPPORT/Blank.prproj.upd" && mv "$SUPPORT/Blank.prproj.upd" "$SUPPORT/Blank.prproj"
fi
# the Dock/Finder icon: swapped into the app, which Finder and the Dock pick up once it's touched
if [ -n "$APP" ] && [ -d "$APP/Contents/Resources" ] && get mac-app/icon/AppIcon.icns \
   && [ "$(head -c 4 "$TMP/AppIcon.icns")" = "icns" ] \
   && ! cmp -s "$TMP/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"; then
  cp "$TMP/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns.upd" \
    && mv "$APP/Contents/Resources/AppIcon.icns.upd" "$APP/Contents/Resources/AppIcon.icns"
  touch "$APP"
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true
  changed="${changed:+$changed, }icon"
fi
if [ -n "$APP" ] && [ -d "$APP/Contents/MacOS" ] && get mac-app/Kickoff.swift \
   && ! cmp -s "$TMP/Kickoff.swift" "$SUPPORT/Kickoff.swift"; then
  if xcrun swiftc -O -o "$TMP/Kickoff" "$TMP/Kickoff.swift" -framework Cocoa -framework WebKit >/dev/null 2>&1; then
    cp "$TMP/Kickoff" "$APP/Contents/MacOS/Kickoff.upd" && mv "$APP/Contents/MacOS/Kickoff.upd" "$APP/Contents/MacOS/Kickoff"
    codesign --force --sign - "$APP" >/dev/null 2>&1 || true
    cp "$TMP/Kickoff.swift" "$SUPPORT/Kickoff.swift"
    changed="${changed:+$changed, }app"
  fi
fi
# RED: build the sound reader against RED's free R3D SDK when it's on this Mac (Downloads or Documents);
# rebuilt when its source changes or the SDK moves. Without the SDK, RED clips are set aside as before.
SDK="$(ls -d "$HOME"/Downloads/R3DSDK* "$HOME"/Documents/R3DSDK* "$HOME"/Applications/R3DSDK* 2>/dev/null \
       | while read -r d; do [ -f "$d/Include/R3DSDK.h" ] && echo "$d"; done | sort | tail -1)"
if [ -n "$SDK" ] && get mac-app/r3d/kickoff_r3d.cpp \
   && { ! cmp -s "$TMP/kickoff_r3d.cpp" "$SUPPORT/kickoff_r3d.cpp" || [ ! -x "$SUPPORT/kickoff_r3d" ] \
        || [ "$(cat "$SUPPORT/kickoff_r3d.sdk" 2>/dev/null)" != "$SDK" ]; }; then
  if xcrun clang++ -O2 -std=c++17 -I"$SDK/Include" "$TMP/kickoff_r3d.cpp" "$SDK/Lib/mac64/libR3DSDK-libcpp.a" \
       -DKICKOFF_DEFAULT_LIBS="\"$SDK/Redistributable/mac\"" -ldl -o "$TMP/kickoff_r3d" >/dev/null 2>&1; then
    cp "$TMP/kickoff_r3d" "$SUPPORT/kickoff_r3d.upd" && mv "$SUPPORT/kickoff_r3d.upd" "$SUPPORT/kickoff_r3d"
    cp "$TMP/kickoff_r3d.cpp" "$SUPPORT/kickoff_r3d.cpp"
    echo "$SDK" > "$SUPPORT/kickoff_r3d.sdk"
    changed="${changed:+$changed, }RED reader"
  fi
fi
# Slop Cut: the frame tagger (Apple Vision, part of macOS), rebuilt when its source changes
if get mac-app/vision/kickoff_vision.swift \
   && { ! cmp -s "$TMP/kickoff_vision.swift" "$SUPPORT/kickoff_vision.swift" || [ ! -x "$SUPPORT/kickoff_vision" ]; }; then
  if xcrun swiftc -O -o "$TMP/kickoff_vision" "$TMP/kickoff_vision.swift" >/dev/null 2>&1; then
    cp "$TMP/kickoff_vision" "$SUPPORT/kickoff_vision.upd" && mv "$SUPPORT/kickoff_vision.upd" "$SUPPORT/kickoff_vision"
    cp "$TMP/kickoff_vision.swift" "$SUPPORT/kickoff_vision.swift"
    changed="${changed:+$changed, }shot finder"
  fi
fi
case "$changed" in
  *window*|*app*|*icon*) echo "Updated: $changed. The new window shows next time you open Kickoff." ;;
  ?*) echo "Updated: $changed" ;;
esac
exit 0

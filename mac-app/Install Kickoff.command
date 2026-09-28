#!/bin/bash
# Installs Kickoff: a Python environment with numpy/scipy, the engine, ffmpeg (via Homebrew if
# missing) and the Kickoff droplet app in ~/Applications. Safe to run again to update.
set -e
cd "$(dirname "$0")"
SUPPORT="$HOME/Library/Application Support/Kickoff"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "== Installing Kickoff =="

if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  echo "Apple's Command Line Tools are needed (they include Python). A dialog will open;"
  echo "click Install, wait for it to finish, then double-click this installer again."
  xcode-select --install || true
  exit 1
fi
PY=/usr/bin/python3
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Python 3.9 or newer is needed."; exit 1; }

mkdir -p "$SUPPORT"
if [ ! -x "$SUPPORT/venv/bin/python3" ]; then
  "$PY" -m venv "$SUPPORT/venv"
fi
"$SUPPORT/venv/bin/python3" -m pip install --quiet --upgrade pip
"$SUPPORT/venv/bin/python3" -m pip install --quiet --upgrade numpy scipy
cp engine/musicsync.py "$SUPPORT/musicsync.py"
cp kickoff-run.sh kickoff-gui-run.sh kickoff-update.sh Kickoff.swift "$SUPPORT/"
chmod +x "$SUPPORT/kickoff-run.sh" "$SUPPORT/kickoff-gui-run.sh" "$SUPPORT/kickoff-update.sh"
mkdir -p "$SUPPORT/ui/fonts"
cp ui/index.html "$SUPPORT/ui/"
cp ui/fonts/* "$SUPPORT/ui/fonts/"
# where automatic updates come from (raw files of the GitHub repository)
echo "https://raw.githubusercontent.com/jakenbake1998/Kickoff/main" > "$SUPPORT/update-url.txt"
echo "Engine installed."

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    echo
    echo "Kickoff needs ffmpeg, which comes from Homebrew. Installing Homebrew now:"
    echo "type your Mac password when asked (nothing shows as you type) and press Return to go on."
    echo
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || {
      echo; echo "Homebrew didn't install. Run this installer again to retry."; exit 1; }
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
  fi
  echo "Installing ffmpeg with Homebrew (a few minutes)..."
  brew install ffmpeg || { echo; echo "ffmpeg didn't install. Run this installer again to retry."; exit 1; }
fi
echo "ffmpeg: $(command -v ffmpeg)"

mkdir -p "$HOME/Applications"
APP="$HOME/Applications/Kickoff.app"
BUILD="$(mktemp -d)/Kickoff.app"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
echo "Building the Kickoff window..."
if xcrun swiftc -O -o "$BUILD/Contents/MacOS/Kickoff" Kickoff.swift -framework Cocoa -framework WebKit; then
  cp Info.plist "$BUILD/Contents/Info.plist"
  cp icon/AppIcon.icns "$BUILD/Contents/Resources/AppIcon.icns"
  codesign --force --sign - "$BUILD" >/dev/null 2>&1 || true
  rm -rf "$APP"
  mv "$BUILD" "$APP"
  touch "$APP"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
else
  # no Swift compiler: fall back to the plain droplet (folder picker + Terminal)
  echo "The window couldn't be built on this Mac; installing the simple version instead."
  rm -rf "$APP"
  osacompile -o "$APP" Kickoff.applescript
fi
# files unzipped from a download carry macOS's quarantine flag; the app is built here, but clear it
# from everything installed so nothing trips Gatekeeper later
xattr -dr com.apple.quarantine "$APP" "$SUPPORT" 2>/dev/null || true
echo
echo "Installed ~/Applications/Kickoff.app"
echo "Drag it to your Dock, then drop a shoot folder on it (or on its window)."
open "$APP"

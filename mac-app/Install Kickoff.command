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
cp kickoff-run.sh "$SUPPORT/kickoff-run.sh"
chmod +x "$SUPPORT/kickoff-run.sh"
# where automatic updates come from (raw files of the GitHub repository)
echo "https://raw.githubusercontent.com/jakenbake1998/Kickoff/main" > "$SUPPORT/update-url.txt"
echo "Engine installed."

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing ffmpeg with Homebrew..."
    brew install ffmpeg
  else
    echo
    echo "ffmpeg is needed and Homebrew isn't installed."
    echo "Install Homebrew from https://brew.sh (one command), then run this installer again."
    open "https://brew.sh"
    exit 1
  fi
fi
echo "ffmpeg: $(command -v ffmpeg)"

mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/Kickoff.app"
osacompile -o "$HOME/Applications/Kickoff.app" Kickoff.applescript
echo
echo "Installed ~/Applications/Kickoff.app"
echo "Drag it to your Dock, then drop a shoot folder on it."
open -R "$HOME/Applications/Kickoff.app"

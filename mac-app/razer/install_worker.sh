#!/bin/bash
# Installs Kickoff's sync worker on the Razer (Linux), so the Mac app can hand it the slow part of a
# sync (Settings > Heavy work). Run it once on the Razer:
#   curl -fsSL https://raw.githubusercontent.com/jakenbake1998/Kickoff/main/mac-app/razer/install_worker.sh | bash
# It keeps itself up to date: before each run the worker fetches the engine from Kickoff's main.
set -e
BASE="https://raw.githubusercontent.com/jakenbake1998/Kickoff/main"
DIR="$HOME/kickoff-worker"
mkdir -p "$DIR"
command -v ffmpeg >/dev/null || { echo "Installing ffmpeg"; sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg; }
command -v python3 >/dev/null || { sudo apt-get install -y -qq python3; }
python3 -m venv "$DIR/venv" 2>/dev/null || { sudo apt-get install -y -qq python3-venv && python3 -m venv "$DIR/venv"; }
"$DIR/venv/bin/pip" install -q --upgrade pip numpy scipy
curl -fsSL "$BASE/mac-app/razer/kickoff_worker.py" -o "$DIR/kickoff_worker.py"
curl -fsSL "$BASE/musicsync.py" -o "$DIR/musicsync.py"
CMD="$DIR/venv/bin/python3 $DIR/kickoff_worker.py"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/kickoff-worker.service" <<UNIT
[Unit]
Description=Kickoff sync worker
After=network-online.target
[Service]
ExecStart=$CMD
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now kickoff-worker.service
  systemctl --user restart kickoff-worker.service
  loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null || true
else
  pkill -f "$DIR/kickoff_worker.py" 2>/dev/null || true
  nohup $CMD >"$DIR/worker.log" 2>&1 &
  ( crontab -l 2>/dev/null | grep -v kickoff_worker.py; echo "@reboot $CMD >$DIR/worker.log 2>&1" ) | crontab -
fi
sleep 3
if curl -fsS http://127.0.0.1:8010/api/kickoff/ping; then
  echo; echo "Kickoff worker is running on port 8010. Mac drives are read from /mnt/mac."
else
  echo "The worker didn't start. Look at $DIR/worker.log or: journalctl --user -u kickoff-worker"
  exit 1
fi

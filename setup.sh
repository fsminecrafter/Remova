#!/usr/bin/env bash
# wifi-keyboard setup: asks for a password, stores only its hash in config.json,
# installs dependencies and a systemd service that starts at boot.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./setup.sh"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/wifi-keyboard

read -rp "Port [8765]: " PORT
PORT="${PORT:-8765}"

while true; do
  read -rsp "Password (min 8 chars): " PW; echo
  read -rsp "Repeat password: " PW2; echo
  [ "$PW" = "$PW2" ] && [ "${#PW}" -ge 8 ] && break
  echo "Passwords differ or are too short. Try again."
done

mkdir -p "$DEST"
cp "$SRC/server.py" "$SRC/index.html" "$DEST/"

echo "Installing dependencies..."
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q aiohttp evdev || {
  echo "evdev failed to build. Install: gcc python3-dev linux-headers-\$(uname -r), then rerun."; exit 1; }

echo "Writing config.json (hash only, no plaintext)..."
FILES_USER="${SUDO_USER:-}"
[ "$FILES_USER" = root ] && FILES_USER=""
PW="$PW" PORT="$PORT" FILES_USER="$FILES_USER" "$DEST/venv/bin/python" - <<'PY'
import hashlib, json, os, secrets
salt = secrets.token_bytes(16)
h = hashlib.scrypt(os.environ["PW"].encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
cfg = {"host": "0.0.0.0", "port": int(os.environ["PORT"]), "n": 2**14,
       "salt": salt.hex(), "hash": h.hex()}
# File explorer starts in the home of the user who ran setup (not root).
if os.environ.get("FILES_USER"):
    cfg["files_user"] = os.environ["FILES_USER"]
with open("/opt/wifi-keyboard/config.json", "w") as f:
    json.dump(cfg, f, indent=2)
PY
chmod 600 "$DEST/config.json"
unset PW PW2

modprobe uinput
echo uinput > /etc/modules-load.d/wifi-keyboard.conf

cat > /etc/systemd/system/wifi-keyboard.service <<EOF
[Unit]
Description=wifi-keyboard
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$DEST/venv/bin/python $DEST/server.py
WorkingDirectory=$DEST
Restart=always
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wifi-keyboard

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. Open http://${IP:-<this-machine>}:$PORT from a device on the same network."
echo "Status: systemctl status wifi-keyboard"

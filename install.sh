#!/usr/bin/env bash
# Instala AirDrop Web como servicio de usuario: arranca solo al iniciar sesión.
# Desinstalar: systemctl --user disable --now airdrop-web && rm ~/.config/systemd/user/airdrop-web.service
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|@DIR@|$DIR|g" "$DIR/airdrop-web.service" > "$UNIT_DIR/airdrop-web.service"
systemctl --user daemon-reload
systemctl --user enable airdrop-web.service
systemctl --user restart airdrop-web.service
sleep 1
systemctl --user --no-pager --lines=0 status airdrop-web.service | head -3
echo
/usr/bin/python3 "$DIR/server.py" url

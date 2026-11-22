#!/usr/bin/env bash
# Install as systemd system service under the current non-root user.
set -euo pipefail
if [[ "$(id -u)" -eq 0 ]]; then
  echo "請用一般使用者執行，不要直接以 root 執行。" >&2
  exit 1
fi
cd -- "$(dirname -- "$(realpath -- "$0")")"
ROOT="$(pwd)"
[[ -f .env ]] || { echo "請先 cp .env.example .env，填好金鑰。" >&2; exit 1; }
chmod 600 .env
USER_NAME="$(id -un)"
NAME="thsr-monitor-${USER_NAME}.service"
SERVICE="/etc/systemd/system/${NAME}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<UNIT
[Unit]
Description=Personal THSR reserved-seat monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${ROOT}
ExecStart=/usr/bin/python3 -u ${ROOT}/thsr_monitor.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
sudo install -m 644 "$TMP" "$SERVICE"
sudo systemctl daemon-reload
sudo systemctl enable --now "$NAME"
echo "已啟用 $NAME"
echo "檢查狀態: sudo systemctl status $NAME"
echo "查看記錄: journalctl -u $NAME -f"
echo "停止: sudo systemctl disable --now $NAME"

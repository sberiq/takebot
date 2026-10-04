#!/usr/bin/env bash
set -Eeuo pipefail

APP_PATH="$(pwd -P)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE_NAME="autotakes"

if [[ "$RUN_USER" == "root" ]]; then
    echo "Запустите скрипт от обычного пользователя с sudo; сам бот не должен работать от root." >&2
    exit 1
fi
if [[ ! -f "$APP_PATH/.env" ]]; then
    echo "Файл .env не найден в $APP_PATH" >&2
    exit 1
fi
if ! grep -Eq '^MAIN_BOT_TOKEN=.+$' "$APP_PATH/.env"; then
    echo "В .env не задан MAIN_BOT_TOKEN." >&2
    exit 1
fi
if ! grep -Eq '^ADMIN_ID=[1-9][0-9]*$' "$APP_PATH/.env"; then
    echo "В .env не задан корректный ADMIN_ID." >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

if [[ ! -x "$APP_PATH/venv/bin/python" ]]; then
    python3 -m venv "$APP_PATH/venv"
fi
"$APP_PATH/venv/bin/python" -m pip install --upgrade pip
"$APP_PATH/venv/bin/python" -m pip install -r "$APP_PATH/requirements.txt"
"$APP_PATH/venv/bin/python" -m compileall -q \
    "$APP_PATH/main.py" \
    "$APP_PATH/main_bot.py" \
    "$APP_PATH/database.py" \
    "$APP_PATH/sub_bot_manager.py" \
    "$APP_PATH/services"

chmod 600 "$APP_PATH/.env"
for protected_file in "$APP_PATH/bot_constructor.db"; do
    if [[ -f "$protected_file" ]]; then
        chmod 600 "$protected_file"
    fi
done

SERVICE_FILE="$(mktemp)"
trap 'rm -f "$SERVICE_FILE"' EXIT
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Self-hosted Telegram Suggestions Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_PATH
EnvironmentFile=$APP_PATH/.env
Environment=PATH=$APP_PATH/venv/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_PATH/venv/bin/python $APP_PATH/main.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$APP_PATH
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo install -o root -g root -m 0644 "$SERVICE_FILE" "/etc/systemd/system/$SERVICE_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl --no-pager --full status "$SERVICE_NAME"

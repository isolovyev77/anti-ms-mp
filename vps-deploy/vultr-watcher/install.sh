#!/bin/bash
# Деплой ИИ-watcher для anti-ms-mp на Vultr.
# Запускается локально: ./install.sh
set -e

VULTR="linuxuser@65.20.114.101"
KEY="$HOME/.ssh/Vultr"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/opt/anti-ms-mp-watcher"
PROJECT_ENV="$LOCAL_DIR/../../.env"

if [ ! -f "$PROJECT_ENV" ]; then
  echo "❌ Не найден $PROJECT_ENV — заполни .env проекта"
  exit 1
fi

echo "== 1. Создаю $REMOTE_DIR на Vultr =="
ssh -i "$KEY" "$VULTR" "sudo mkdir -p $REMOTE_DIR && sudo chown linuxuser:linuxuser $REMOTE_DIR"

echo "== 2. Копирую watcher.sh и .env =="
scp -i "$KEY" "$LOCAL_DIR/watcher.sh" "$VULTR:$REMOTE_DIR/watcher.sh"
scp -i "$KEY" "$PROJECT_ENV" "$VULTR:$REMOTE_DIR/.env"
ssh -i "$KEY" "$VULTR" "chmod 600 $REMOTE_DIR/.env; chmod +x $REMOTE_DIR/watcher.sh"

echo "== 3. Smoke-test (с принудительной отправкой) =="
ssh -i "$KEY" "$VULTR" "WATCHER_FORCE_NOTIFY=1 $REMOTE_DIR/watcher.sh 2>&1 | tail -30"

echo "== 4. Ставлю cron на ежедневно 06:00 UTC (09:00 МСК) =="
ssh -i "$KEY" "$VULTR" bash -s <<EOF
CRON_LINE="0 6 * * * $REMOTE_DIR/watcher.sh >> $REMOTE_DIR/watcher.log 2>&1"
( crontab -l 2>/dev/null | grep -v "anti-ms-mp-watcher" ; echo "\$CRON_LINE" ) | crontab -
crontab -l | grep anti-ms-mp-watcher
EOF

echo "✅ Watcher развёрнут. Логи: ssh -i $KEY $VULTR 'tail -f $REMOTE_DIR/watcher.log'"

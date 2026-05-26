#!/bin/bash
# Деплой парсера anti-ms-mp на VDSina.
# Запускается локально, не на VDSina: ./install.sh
#
# Что делает:
#   1. ssh на VDSina, ставит python3-venv, создаёт /opt/anti-ms-mp
#   2. Копирует parser_runner.py + .env (с твоего ноута)
#   3. Создаёт venv, ставит cloakbrowser
#   4. Делает первый прогон (smoke-test, один платформа avito с 1 страницей)
#   5. Если smoke-test ок — ставит cron каждые 6 часов

set -e

VDSINA="root@94.103.89.251"
KEY="$HOME/.ssh/VDSina"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/opt/anti-ms-mp"
PROJECT_ENV="$LOCAL_DIR/../../.env"

if [ ! -f "$PROJECT_ENV" ]; then
  echo "❌ Локальный .env не найден: $PROJECT_ENV"
  echo "   Заполни .env (особенно SUPABASE_SERVICE_ROLE_KEY) и повтори"
  exit 1
fi

if ! grep -q "^SUPABASE_SERVICE_ROLE_KEY=." "$PROJECT_ENV"; then
  echo "❌ SUPABASE_SERVICE_ROLE_KEY в .env пустой"
  exit 1
fi

echo "== 1. Подготавливаю VDSina =="
ssh -i "$KEY" "$VDSINA" bash -s <<EOF
set -e
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl
mkdir -p $REMOTE_DIR
EOF

echo "== 2. Копирую parser_runner.py и .env =="
scp -i "$KEY" "$LOCAL_DIR/parser_runner.py" "$VDSINA:$REMOTE_DIR/"
scp -i "$KEY" "$PROJECT_ENV" "$VDSINA:$REMOTE_DIR/.env"
ssh -i "$KEY" "$VDSINA" "chmod 600 $REMOTE_DIR/.env"

echo "== 3. Ставлю CloakBrowser в venv =="
ssh -i "$KEY" "$VDSINA" bash -s <<EOF
set -e
cd $REMOTE_DIR
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install cloakbrowser --quiet
.venv/bin/python -c "from cloakbrowser import launch; print('cloakbrowser OK')"
EOF

echo "== 4. Smoke-test (avito, 1 страница) =="
ssh -i "$KEY" "$VDSINA" "cd $REMOTE_DIR && .venv/bin/python parser_runner.py --platforms avito --trigger manual 2>&1 | tail -20"

echo "== 5. Ставлю cron каждые 6 часов =="
ssh -i "$KEY" "$VDSINA" bash -s <<'EOF'
CRON_LINE="0 */6 * * * cd /opt/anti-ms-mp && .venv/bin/python parser_runner.py --trigger cron >> /var/log/anti-ms.log 2>&1"
( crontab -l 2>/dev/null | grep -v "anti-ms-mp" ; echo "$CRON_LINE" ) | crontab -
crontab -l | grep anti-ms-mp
EOF

echo "✅ Деплой завершён. Парсер будет запускаться каждые 6 часов."
echo "   Логи: ssh root@94.103.89.251 'tail -f /var/log/anti-ms.log'"

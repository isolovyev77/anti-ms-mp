#!/bin/bash
# ИИ-watcher для anti-ms-mp.
# Запускается из cron на Vultr ежедневно (например, 09:00 МСК = 06:00 UTC).
# Проверяет parser_runs за последние 7 дней, анализирует через Claude CLI,
# и если есть аномалии — шлёт в Telegram через n8n webhook.
#
# ENV (берётся из /opt/anti-ms-mp-watcher/.env):
#   SUPABASE_URL
#   SUPABASE_ANON_KEY      (publishable — достаточно для чтения)
#   N8N_WEBHOOK_PARSER_DONE (если пусто — просто залогируем)
#   TG_CHAT_ID             (для webhook payload)
#
# Cron:
#   0 6 * * * /opt/anti-ms-mp-watcher/watcher.sh >> /opt/anti-ms-mp-watcher/watcher.log 2>&1

set -e
cd "$(dirname "$0")"
ENV_FILE="$(dirname "$0")/.env"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_ANON_KEY" ]; then
  echo "[$(date -Iseconds)] FATAL: SUPABASE_URL/SUPABASE_ANON_KEY не заданы"
  exit 1
fi

TS=$(date -Iseconds)
WEEK_AGO=$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%S)
RUNS_JSON=$(curl -sS --max-time 30 \
  "$SUPABASE_URL/rest/v1/parser_runs?select=*&started_at=gte.$WEEK_AGO&order=started_at.desc" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $SUPABASE_ANON_KEY")

if [ -z "$RUNS_JSON" ] || [ "$RUNS_JSON" = "[]" ]; then
  echo "[$TS] No parser_runs в последние 7 дней — алерт"
  REPORT="🔔 Watcher: за последние 7 дней не было ни одного запуска парсера. Проверьте cron на VDSina."
else
  # Кратенькая сводка для Claude
  SUMMARY=$(echo "$RUNS_JSON" | python3 -c "
import json, sys
runs = json.load(sys.stdin)
print(f'Всего прогонов: {len(runs)}')
by_status = {}
for r in runs:
    by_status[r['status']] = by_status.get(r['status'], 0) + 1
print('По статусам:', by_status)
print('Последние 5:')
for r in runs[:5]:
    totals = r.get('totals') or {}
    plat = ', '.join(f\"{k}={v.get('upserted',0)}\" for k,v in totals.items()) or '(нет)'
    err = len(r.get('errors') or [])
    print(f\"  #{r['id']} {r['started_at'][:16]} status={r['status']} trigger={r['trigger']} платформы=[{plat}] err={err}\")
")
  echo "[$TS] Summary:"
  echo "$SUMMARY"

  # Передаём в Claude для анализа
  REPORT=$(claude --print --output-format text 2>/dev/null << EOF || echo "Claude не отработал — сводка ниже\n$SUMMARY"
Ты — мониторинг-агент для проекта anti-ms-mp (парсер маркетплейсов на VDSina, cron каждые 6 часов).

Вот сводка по parser_runs за последние 7 дней:
$SUMMARY

Сделай короткий русскоязычный анализ (до 600 символов):
1) Всё ли в порядке? (есть ли парсинг каждые 6ч, успешные ли прогоны)
2) Есть ли деградация по платформам (резкое падение upserted)?
3) Что насторожило?

Если всё хорошо — просто скажи «✅ Парсер работает штатно, прогонов NN, замечаний нет». Без воды.
Если что-то не так — начни с ⚠️ или ❌ и опиши конкретно.
EOF
)
fi

echo "[$TS] Report:"
echo "$REPORT"

# Отправляем в Telegram только если есть ⚠️/❌ или явный алерт
SHOULD_NOTIFY=0
case "$REPORT" in
  *⚠️*|*❌*|*"FATAL"*|*"deg"*|*"проблем"*|*"оширб"*) SHOULD_NOTIFY=1 ;;
esac

# В первом запуске — всегда уведомить (чтобы протестировать)
[ -n "$WATCHER_FORCE_NOTIFY" ] && SHOULD_NOTIFY=1

if [ "$SHOULD_NOTIFY" = "1" ] && [ -n "$N8N_WEBHOOK_PARSER_DONE" ] && [ -n "$TG_CHAT_ID" ]; then
  curl -sS --max-time 15 --resolve is77.duckdns.org:443:127.0.0.1 -X POST "https://is77.duckdns.org/webhook/antimsmp-parser-done" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c "
import json, os
print(json.dumps({
  'run_id': 'watcher',
  'status': 'watcher',
  'totals': {},
  'errors_count': 0,
  'chat_id': int(os.environ['TG_CHAT_ID']),
  'text_override': os.environ.get('REPORT_BODY','')
}, ensure_ascii=False))
" REPORT_BODY="$REPORT")" | head -c 300
  echo
elif [ "$SHOULD_NOTIFY" = "1" ]; then
  echo "[$TS] Алерт есть, но N8N_WEBHOOK_PARSER_DONE/TG_CHAT_ID пустые — пропуск отправки"
fi
echo "[$TS] === watcher done ==="

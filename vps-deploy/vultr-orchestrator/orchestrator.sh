#!/bin/bash
# Оркестратор anti-ms-mp — ночной дежурный (Vultr, cron 01:00 UTC = 04:00 МСК).
#
# Парсер на VDSina отрабатывает в 00:00 МСК БЕЗ своего Telegram-сообщения
# (--no-notify). Этот оркестратор в 04:00:
#   1) healthcheck.py — сверяет «нашли» vs «видно на дашборде» по каждой площадке,
#      ловит класс «нашли 400 — показываем 0» (сломанный съём цены/вёрстка);
#   2) Claude (sonnet) пишет ОДИН понятный русский отчёт;
#   3) отчёт уходит в Telegram через n8n (text_override).
#
# Так получается единственное утреннее сообщение — понятное, на русском, со здоровьем.
#
# ENV (/opt/anti-ms-mp-watcher/.env): SUPABASE_URL, SUPABASE_ANON_KEY,
#   N8N_WEBHOOK_PARSER_DONE, TG_CHAT_ID
#
# Cron: 0 1 * * * /opt/anti-ms-mp-watcher/orchestrator.sh >> /opt/anti-ms-mp-watcher/orchestrator.log 2>&1
set -e
cd "$(dirname "$0")"
ENV_FILE="$(dirname "$0")/.env"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
CLAUDE=/home/linuxuser/.local/bin/claude
TS=$(date -Iseconds)

if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_ANON_KEY" ]; then
  echo "[$TS] FATAL: SUPABASE_URL/SUPABASE_ANON_KEY не заданы"; exit 1
fi

# 1) Детектор аномалий
HEALTH=$(python3 "$(dirname "$0")/healthcheck.py" 2>/dev/null) || HEALTH='{"error":"healthcheck упал"}'
echo "[$TS] health: $HEALTH"

HEALTHY=$(echo "$HEALTH" | python3 -c "import json,sys;print(json.load(sys.stdin).get('healthy'))" 2>/dev/null || echo "None")

# 2) Claude пишет утренний отчёт из health-JSON
PROMPT="Ты — утренний дежурный по мониторингу признаков контрафакта ПО на маркетплейсах.
Вот результат ночной проверки (JSON): сколько карточек парсер НАШЁЛ (found) и сколько
РЕАЛЬНО видно контрафакта на дашборде (counterfeit), с покрытием ценой (coverage).

$HEALTH

Напиши ОДИН короткий отчёт на русском для Telegram (до 700 символов, без markdown-таблиц):
- Первой строкой — итог по площадкам: «Площадка: N контрафакта (нашли M)».
- Если healthy=true — заверши строкой «✅ Все площадки в норме».
- Если есть anomalies — начни с «⚠️», по каждой опиши простыми словами что не так,
  вероятную причину (смена вёрстки/сломан съём цены) и что инженеру проверить.
Пиши деловым русским языком, без англицизмов вроде upserted/coverage, без воды."

REPORT=$("$CLAUDE" --print --output-format text 2>/dev/null <<<"$PROMPT" || true)

# Фолбэк, если Claude недоступен — простой шаблон из JSON
if [ -z "$REPORT" ]; then
  REPORT=$(echo "$HEALTH" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except: print('Не удалось получить данные проверки.'); sys.exit()
ru={'ozon':'Ozon','wildberries':'Wildberries','yandex':'Яндекс.Маркет','avito':'Avito'}
lines=['Утренний отчёт мониторинга:']
for p in d.get('platforms',[]):
    lines.append(f\"{ru.get(p['pl'],p['pl'])}: {p['counterfeit']} контрафакта (нашли {p['found']})\")
an=d.get('anomalies') or []
if not an: lines.append('✅ Все площадки в норме')
else:
    lines.append('⚠️ Аномалии:')
    for a in an: lines.append('• '+a['detail'])
print(chr(10).join(lines))
")
fi
echo "[$TS] report:"; echo "$REPORT"

# 3) Отправка в Telegram (всегда — это утреннее сообщение дня)
if [ -n "$DRYRUN" ]; then
  echo "[$TS] DRYRUN — отчёт НЕ отправлен (тест)"
elif [ -n "$TG_CHAT_ID" ]; then
  curl -sS --max-time 15 --resolve is77.duckdns.org:443:127.0.0.1 \
    -X POST "https://is77.duckdns.org/webhook/antimsmp-parser-done" \
    -H 'Content-Type: application/json' \
    -d "$(REPORT_BODY="$REPORT" python3 -c "
import json, os
print(json.dumps({'run_id':'orchestrator','status':'orchestrator','totals':{},
  'errors_count':0,'chat_id':int(os.environ['TG_CHAT_ID']),
  'text_override':os.environ.get('REPORT_BODY','')}, ensure_ascii=False))")" | head -c 200
  echo
else
  echo "[$TS] TG_CHAT_ID пуст — отчёт только в лог"
fi
echo "[$TS] === orchestrator done (healthy=$HEALTHY) ==="

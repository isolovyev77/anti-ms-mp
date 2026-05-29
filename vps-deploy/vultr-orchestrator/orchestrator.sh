#!/bin/bash
# Оркестратор anti-ms-mp — ночной дежурный (Vultr, cron 01:00 UTC = 04:00 МСК).
#
# Парсер на VDSina отрабатывает в 00:00 МСК БЕЗ своего Telegram (--no-notify).
# В 04:00 оркестратор:
#   1) healthcheck.py — сверяет «нашли» vs «видно контрафакта», ловит класс
#      «нашли 400 — показываем 0» (сломанный съём цены после смены вёрстки);
#   2) АВТО-ПОЧИНКА: при аномалии Claude(sonnet) заходит на VDSina, диагностирует
#      через diag_price.py, правит EXTRACT_JS с бэкапом, перезапускает площадку,
#      проверяет покрытие, откатывает если не помогло;
#   3) пишет ОДИН русский отчёт (прогон + здоровье + что чинил) → Telegram.
#
# ENV (/opt/anti-ms-mp-watcher/.env): SUPABASE_URL, SUPABASE_ANON_KEY,
#   N8N_WEBHOOK_PARSER_DONE, TG_CHAT_ID
# Флаги: DRYRUN=1 — не слать в Telegram; AUTOFIX=0 — только детект+алерт.
#
# Cron: 0 1 * * * /opt/anti-ms-mp-watcher/orchestrator.sh >> /opt/anti-ms-mp-watcher/orchestrator.log 2>&1
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
[ -f "$HERE/.env" ] && set -a && . "$HERE/.env" && set +a
CLAUDE=/home/linuxuser/.local/bin/claude
VDSINA_KEY="$HERE/vdsina_key"
SSH_VDSINA="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i $VDSINA_KEY root@94.103.89.251"
TS=$(date -Iseconds)
TODAY=$(TZ=Europe/Moscow date +%F)
QUERY_DEFAULT="Microsoft Office 2021 ключ"

if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_ANON_KEY" ]; then
  echo "[$TS] FATAL: SUPABASE_URL/SUPABASE_ANON_KEY не заданы"; exit 1
fi

# 1) Детектор аномалий
HEALTH=$(python3 "$HERE/healthcheck.py" 2>/dev/null) || HEALTH='{"error":"healthcheck упал","anomalies":[]}'
echo "[$TS] health: $HEALTH"
ANOMALY_PLS=$(echo "$HEALTH" | python3 -c "import json,sys
try: print(' '.join(a['pl'] for a in json.load(sys.stdin).get('anomalies',[])))
except: pass" 2>/dev/null)

# 2) Авто-починка (если есть аномалии и не выключено)
FIXLOG=""
if [ -n "$ANOMALY_PLS" ] && [ "$AUTOFIX" != "0" ]; then
  for PL in $ANOMALY_PLS; do
    DETAIL=$(echo "$HEALTH" | python3 -c "import json,sys
d=json.load(sys.stdin)
print(next((a['detail'] for a in d['anomalies'] if a['pl']=='$PL'),''))" 2>/dev/null)
    echo "[$TS] AUTOFIX старт для $PL: $DETAIL"
    FIX_PROMPT="Ты — инженер-наладчик парсера маркетплейсов anti-ms-mp. Обнаружена аномалия данных:
«$DETAIL»

Парсер: VDSina, /opt/anti-ms-mp/parser_runner.py. Логика извлечения карточек — в словаре
EXTRACT_JS[\"$PL\"] (JS, выполняется на странице выдачи). Аномалия «нашли много — цена не
извлекается» обычно значит, что маркетплейс сменил вёрстку и CSS-селекторы устарели.

Команды на VDSina выполняй через Bash, префикс:
  SSH=\"$SSH_VDSINA\"

СТРОГАЯ МЕТОДИКА:
1) ДИАГНОЗ. Запусти готовую диагностику (свою НЕ пиши):
   \$SSH 'cd /opt/anti-ms-mp && timeout 120 .venv/bin/python diag_price.py $PL \"$QUERY_DEFAULT\"'
   Сравни current_extractor_output (что берёт парсер) с dom_price_elements (где цена реально).
   Если current даёт price 0/null, а в dom есть «<число> ₽» — селектор цены умер.
2) ПОЧИНКА. Сначала бэкап:
   \$SSH 'cp /opt/anti-ms-mp/parser_runner.py /opt/anti-ms-mp/parser_runner.py.autofix.bak'
   Затем минимально поправь ТОЛЬКО блок EXTRACT_JS[\"$PL\"] под новый DOM (например искать
   первый листовой элемент с текстом, начинающимся на «<число> ₽»). Меняй только извлечение
   цены этой площадки. Правь через python/sed по SSH. После правки ОБЯЗАТЕЛЬНО проверь синтаксис:
   \$SSH 'cd /opt/anti-ms-mp && .venv/bin/python -m py_compile parser_runner.py && echo SYNTAX_OK'
   Если синтаксис сломан — откати из .autofix.bak и не продолжай.
3) ПРОВЕРКА. Перепарси площадку:
   \$SSH 'cd /opt/anti-ms-mp && nohup timeout 300 .venv/bin/python parser_runner.py --platforms $PL --no-notify > /tmp/autofix_$PL.log 2>&1 &'
   Опрашивай \$SSH 'grep -c \"run end\" /tmp/autofix_$PL.log' пока не появится (макс ~5 мин).
   Покрытие ценой через Supabase:
   curl -s \"\$SUPABASE_URL/rest/v1/listings?select=price&pl=eq.$PL&last_seen=eq.$TODAY&price=gt.0\" -H \"apikey: \$SUPABASE_ANON_KEY\" -H \"Authorization: Bearer \$SUPABASE_ANON_KEY\" -H \"Prefer: count=exact\" -I | grep -i content-range
4) РЕШЕНИЕ. Если карточек с ценой стало заметно больше (десятки+) — ОСТАВЬ правку. Если нет —
   ОТКАТИ: \$SSH 'cp /opt/anti-ms-mp/parser_runner.py.autofix.bak /opt/anti-ms-mp/parser_runner.py'
5) Если current_extractor_output УЖЕ содержит нормальные цены — значит экстрактор работает,
   НИЧЕГО не меняй, отчитайся «правка не требуется».

ЖЁСТКО: не трогай другие площадки/файлы; не удаляй данные; не меняй cron; при сомнении —
откати и напиши, что нужна ручная проверка.

ОТВЕТ: верни ОДИН короткий абзац на русском (≤400 символов) — что было сломано, что изменил
(или «правка не требуется»), результат проверки (цены до/после), оставил или откатил. Без markdown."

    FIXOUT=$(cd "$HERE" && timeout 1000 "$CLAUDE" --print --dangerously-skip-permissions --model sonnet 2>/dev/null <<<"$FIX_PROMPT" || echo "авто-починка не завершилась за отведённое время — нужна ручная проверка")
    echo "[$TS] AUTOFIX итог $PL: $FIXOUT"
    FIXLOG="$FIXLOG
• $PL: $FIXOUT"
  done
  # пересчёт здоровья после починки
  HEALTH=$(python3 "$HERE/healthcheck.py" 2>/dev/null) || true
  echo "[$TS] health после починки: $HEALTH"
fi

# 3) Claude пишет утренний отчёт (прогон + здоровье + что чинил)
PROMPT="Ты — утренний дежурный мониторинга признаков контрафакта ПО на маркетплейсах.
Результат ночной проверки (JSON: found — сколько нашли, counterfeit — сколько видно контрафакта):
$HEALTH

Что делала авто-починка этой ночью (пусто = аномалий не было):${FIXLOG:- нет}

Напиши ОДИН отчёт на русском для Telegram (до 800 символов, без markdown-таблиц):
- Первой строкой по площадкам: «Площадка: N контрафакта (нашли M)».
- Если аномалий не было — заверши «✅ Все площадки в норме».
- Если была починка — отдельным блоком «🔧 Авто-починка:» простыми словами: что было сломано
  и чем закончилось (починено/откачено/нужна ручная проверка).
Деловой русский, без англицизмов (upserted/coverage), без воды."
REPORT=$(cd "$HERE" && "$CLAUDE" --print --output-format text 2>/dev/null <<<"$PROMPT" || true)

# Фолбэк без Claude
if [ -z "$REPORT" ]; then
  REPORT=$(echo "$HEALTH" | FIXLOG="$FIXLOG" python3 -c "
import json,sys,os
try: d=json.load(sys.stdin)
except: print('Не удалось получить данные ночной проверки.'); sys.exit()
ru={'ozon':'Ozon','wildberries':'Wildberries','yandex':'Яндекс.Маркет','avito':'Avito'}
L=['Утренний отчёт мониторинга:']
for p in d.get('platforms',[]):
    L.append(f\"{ru.get(p['pl'],p['pl'])}: {p['counterfeit']} контрафакта (нашли {p['found']})\")
an=d.get('anomalies') or []
L.append('✅ Все площадки в норме' if not an else '⚠️ Аномалии: '+'; '.join(a['detail'] for a in an))
fx=os.environ.get('FIXLOG','').strip()
if fx: L.append('🔧 Авто-починка:'+fx)
print(chr(10).join(L))")
fi
echo "[$TS] report:"; echo "$REPORT"

# 4) Отправка в Telegram
if [ -n "$DRYRUN" ]; then
  echo "[$TS] DRYRUN — отчёт НЕ отправлен"
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
echo "[$TS] === orchestrator done ==="

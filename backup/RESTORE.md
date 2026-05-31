# anti-ms-mp — восстановление с нуля

Снимок боевой системы на 2026-05-29. Позволяет поднять всё заново при потере
VPS, GitHub/Gitverse или Supabase. **Секреты лежат в папке проекта, но НЕ в git** —
`backup/secrets/` (`vdsina.env`, `vultr.env`, `oracle-watch.env`) в .gitignore, поэтому локально доступны,
но никогда не пушатся на GitHub. (Дубль — в `~/anti-ms-mp-secrets/`.)

## Архитектура (что где крутится)

| Компонент | Где | Назначение |
|---|---|---|
| Парсер | VDSina `94.103.89.251` `/opt/anti-ms-mp/` | cron 00:00 МСК, скрейпит Ozon/WB/Яндекс/Avito → Supabase |
| Оркестратор | Vultr `65.20.114.101` `/opt/anti-ms-mp-watcher/` | cron 04:00 МСК, health-check + авто-починка + утренний отчёт |
| База | Supabase проект `yqfdbuiyfkzhkhpiknob` (eu-west-1) | таблицы listings/parser_runs/monitor_queries/parse_queue/daily_stats |
| Дашборд | Vercel `anti-ms-mp.vercel.app` ← репозиторий (корень → каталог) | `anti-ms-dashboard/index_new.html` (тёмный командный центр) |
| Уведомления | n8n на `is77.duckdns.org` (Vultr) | webhook `/webhook/antimsmp-parser-done` → Telegram (chat_id 18182975) |
| RU-IP / прокси | VDSina | резидентный РФ-IP для скрейпинга |
| Сторож (dead-man) | Oracle `158.180.14.236` `~/anti-ms-watch/` (clawbot) | cron ежечасно, алерт в Telegram напрямую если оркестратор/парсер замолчали (#18) |

SSH-ключи: `~/.ssh/VDSina`, `~/.ssh/Vultr` (на этом ноутбуке). Доступы — в памяти Claude
`reference_vps_inventory`. Ключевая фраза RPC — в `~/anti-ms-mp-secrets/` и в .env.

## Что в этом бэкапе

- `vdsina/` — боевой код парсера: `parser_runner.py`, `watchdog.py`, `queue_poller.py`,
  `enrich_avito_pay_db.py`, `diag_price.py` + `crontab.txt`
- `vultr/` — оркестратор: `orchestrator.sh`, `healthcheck.py`, старый `watcher.sh` + `crontab.txt`
- `db/schema.sql` — таблицы, индексы, RLS, RPC-функции (ключевая фраза → плейсхолдер)
- `~/anti-ms-mp-secrets/*.env` — **вне git**: реальные ключи (service_role, прокси, n8n, фраза)
- дашборд — в самом репозитории `anti-ms-dashboard/`

## Восстановление

### 1. Supabase
1. Создать проект (или восстановить из бэкапа Supabase, если есть).
2. SQL Editor → выполнить `db/schema.sql`.
3. Подставить реальную ключевую фразу вместо `<<PARSE_KEY_PHRASE>>` в обеих RPC
   (значение — в `~/anti-ms-mp-secrets/`).
4. Данные: либо восстановить дамп таблиц, либо запустить парсер — наполнит заново.
5. Взять новые ключи проекта (URL, anon/publishable, service_role) → в .env (см. ниже).

### 2. Парсер (VDSina)
```bash
ssh -i ~/.ssh/VDSina root@94.103.89.251
mkdir -p /opt/anti-ms-mp && cd /opt/anti-ms-mp
python3 -m venv .venv && .venv/bin/pip install cloakbrowser   # + playwright deps
# залить из backup/vdsina/: *.py
# залить ~/anti-ms-mp-secrets/vdsina.env → /opt/anti-ms-mp/.env (chmod 600)
crontab backup/vdsina/crontab.txt   # 00:00 парсер, */5 watchdog, * poller, 01:00 enrich
# #5: непривилегированный юзер для доступа оркестратора (вместо root с Vultr):
useradd -m -s /bin/bash orch          # БЕЗ sudo
mkdir -p /home/orch/.ssh && chmod 700 /home/orch/.ssh
# вписать publkey vdsina_orch.pub (генерится на Vultr, см. секцию 3), ограничить IP Vultr:
echo 'from="65.20.114.101" <содержимое vdsina_orch.pub>' > /home/orch/.ssh/authorized_keys
chmod 600 /home/orch/.ssh/authorized_keys && chown -R orch:orch /home/orch/.ssh
chown -R orch:orch /opt/anti-ms-mp     # orch владеет каталогом парсера (запуск/правка/логи)
```
Проверка: `.venv/bin/python parser_runner.py --trigger manual --platforms yandex`

### 3. Оркестратор (Vultr)
```bash
ssh -i ~/.ssh/Vultr linuxuser@65.20.114.101
mkdir -p /opt/anti-ms-mp-watcher && cd /opt/anti-ms-mp-watcher
# залить из backup/vultr/: orchestrator.sh, healthcheck.py, logmem.py
# залить ~/anti-ms-mp-secrets/vultr.env → .env (chmod 600); проставить AUTOFIX=0/1
# #5: оркестратор ходит на VDSina под orch (НЕ root). Сгенерировать restricted-ключ:
ssh-keygen -t ed25519 -f vdsina_orch -N "" && chmod 600 vdsina_orch
#   → публичную часть vdsina_orch.pub добавить юзеру orch на VDSina (секция 2)
# #13: зафиксировать host-key VDSina (иначе StrictHostKeyChecking=yes не пустит):
ssh-keyscan 94.103.89.251 > known_hosts
# установить Claude Code CLI (claudeclaw) и авторизовать; модель sonnet
crontab backup/vultr/crontab.txt   # 0 1 * * * orchestrator.sh (04:00 МСК)
```
Проверка: `DRYRUN=1 ./orchestrator.sh`

### 4. Дашборд (Vercel)
- Репозиторий деплоится как есть (корневой `index.html` = каталог, ссылки на
  `anti-ms-dashboard/`). Боевой дашборд — `anti-ms-dashboard/index_new.html`.
- Подставить актуальные `SUPABASE_URL` + publishable-ключ в начале `<script>` дашборда.

### 5. n8n (уведомления) — опционально
- Workflow с webhook `POST /webhook/antimsmp-parser-done`, который берёт поле
  `text_override` (или собирает из `totals/status`) и шлёт в Telegram (бот + chat_id).
- Оркестратор уже шлёт готовый русский текст в `text_override` — n8n достаточно
  переслать его в Telegram.

### 6. Независимый сторож (Oracle) — dead-man's switch #18
```bash
ssh -i ~/.ssh/clawbot_key clawbot@158.180.14.236   # Oracle free-tier, 24/7
mkdir -p ~/anti-ms-watch
# залить vps-deploy/oracle-watch/watch.py → ~/anti-ms-watch/watch.py
# залить backup/secrets/oracle-watch.env → ~/anti-ms-watch/.env (chmod 600)
#   .env: IS_N8N_TELEGRAM_BOT_TOKEN, TG_CHAT_ID, SUPABASE_URL, SUPABASE_ANON_KEY
( crontab -l 2>/dev/null; echo "17 * * * * cd ~/anti-ms-watch && /usr/bin/python3 watch.py >> ~/anti-ms-watch/watch.log 2>&1" ) | crontab -
```
Читает Supabase `system_heartbeats[orchestrator]` + свежесть `parser_runs`; при простое
>25ч (оркестратор) / >36ч (парсер) шлёт алерт НАПРЯМУЮ в Telegram (бот is_n8n_bot, минуя
n8n/Vultr → переживает смерть Vultr). Отметку живости пишет оркестратор через RPC
`record_heartbeat` (есть в `db/schema.sql`). Тест канала: `python3 watch.py --test`.

## Зависимости, которые НЕ в этом бэкапе (восстановить отдельно)
- Аккаунт Supabase и сам проект (или его бэкап).
- Аккаунт Vercel + привязка репозитория.
- n8n-инстанс на Vultr (docker) + Telegram-бот токен (в n8n credentials).
- Claude Code CLI на Vultr + авторизация (для оркестратора/авто-починки).
- cloakbrowser + chromium на VDSina.
- Oracle free-tier инстанс (158.180.14.236) + юзер `clawbot` — для независимого сторожа #18.

## Обновление бэкапа
Перетянуть боевые файлы: `tar` по SSH (см. историю) или вручную scp в `backup/{vdsina,vultr}/`,
обновить `~/anti-ms-mp-secrets/*.env`, переснять `db/schema.sql`.

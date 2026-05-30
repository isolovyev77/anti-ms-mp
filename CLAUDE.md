# anti-ms-mp — контекст для ИИ-агентов проекта

Мониторинг признаков контрафакта MS Office на маркетплейсах (Ozon, Wildberries,
Яндекс.Маркет, Avito). Парсер на VDSina → Supabase → дашборд на Vercel.
Оркестратор на Vultr следит за здоровьем и чинит сбои автономно.

## ⚠️ ПЕРВЫМ ДЕЛОМ: общий оперативный лог

Перед любыми действиями по проекту **прочитай общий лог координации** — чтобы не
дублировать уже сделанное другими агентами и заметить пометки `NEEDS-CHECK`:

```bash
ssh -i ~/.ssh/Vultr linuxuser@65.20.114.101 'tail -40 /opt/anti-ms-mp-watcher/memory.md'
```

После значимого действия (фикс, перезапуск, расследование) **допиши в лог**:

```bash
ssh -i ~/.ssh/Vultr linuxuser@65.20.114.101 \
  'python3 /opt/anti-ms-mp-watcher/logmem.py claude <LEVEL> "что сделал"'
# LEVEL: INFO | FIX | WARN | ERROR | NEEDS-CHECK | RESOLVED
```

Лог сам ротируется (последние 200 строк) и дедуплицирует подряд идущие строки.
Его читают/пишут: оркестратор (Vultr, 04:00 МСК), утренняя рутина (ноутбук, 09:13),
и сессии Claude Code.

## Инфраструктура

| Узел | Доступ | Назначение |
|---|---|---|
| VDSina `94.103.89.251` | `ssh -i ~/.ssh/VDSina root@…` | парсер `/opt/anti-ms-mp/`, cron 00:00 МСК |
| Vultr `65.20.114.101` | `ssh -i ~/.ssh/Vultr linuxuser@…` | оркестратор `/opt/anti-ms-mp-watcher/`, cron 04:00 МСК; n8n |
| Supabase | проект `yqfdbuiyfkzhkhpiknob` | таблицы listings/parser_runs/monitor_queries/parse_queue/daily_stats |
| Vercel | `anti-ms-mp.vercel.app/anti-ms-dashboard/index_new.html` | боевой дашборд (из этого репо) |

Секреты — НЕ в git: `backup/secrets/*.env` (gitignored) + `~/anti-ms-mp-secrets/`.
Восстановление с нуля: `backup/RESTORE.md`. Схема БД: `backup/db/schema.sql`.

## Ключевые правила (выучены на граблях)

- **Никакого MIN_PRICE-фильтра**: цены 18–29 ₽ — реальный контрафакт (корп. ключи), главный сигнал.
- **`.env` в git-папке = риск утечки** на GitHub (репо пушится). Секреты только в gitignored/вне репо.
- **Прод-операции** (миграции БД, флип AUTOFIX, запуск headless-агента с `--dangerously-skip-permissions`)
  — только с явного согласия владельца; авто-классификатор Claude Code их блокирует не зря.
- Парсер укреплён от зависаний: heartbeat по прогрессу, яндекс последним, daily_stats инкрементально.
- Дашборд при сбое площадки показывает её ПОСЛЕДНИЕ данные (не пустоту) с пометкой устаревания.

## Где ещё контекст

- Журналы сессий: Obsidian `AI-Sessions/anti-ms-mp/` + MemPalace `wing_anti_ms_mp`.
- Структура деплоя и код: `vps-deploy/{vdsina-parser,vultr-orchestrator}/`.

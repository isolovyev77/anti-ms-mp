# Архитектура anti-ms-dashboard — моё предложение
**Автор:** Claude Sonnet 4.6
**Дата:** 2026-03-22
**На основе:** анализ проекта + опыт сессий 21-22 марта 2026

---

## TL;DR

Git + JSON + GitVerse/GitHub Pages — правильная основа. Главная нерешённая проблема Codex: **Ozon блокирует не-российские IP**, поэтому CI-runner должен иметь российский IP. GitVerse CI (Сбер, российская инфраструктура) — первый кандидат для проверки. Supabase полезен для аналитики в разработке, но в production pipeline не нужен.

---

## Принципы

1. **Дашборд — чистая статика.** `index.html` не знает данных, только схему. Данные = `data/latest.json`.
2. **Git — единственное хранилище.** Нет внешних БД в критическом пути. История обновлений — в git-коммитах.
3. **Scraping — с российского IP.** Без этого Ozon (самый богатый источник) недоступен.
4. **Один entrypoint.** Один shell-скрипт запускает весь pipeline: scrape → compile → commit → push.

---

## Ключевое отличие от предложения Codex

Codex предлагает GitHub Actions как primary scheduler. Это не работает для Ozon:

| CI-платформа | IP-адрес | Ozon |
|-------------|----------|------|
| GitHub Actions | США (datacenter) | ❌ 403 |
| GitVerse CI | Россия (Сбер infra) | ✅ вероятно |
| Российский VPS | Россия | ✅ |

**Первый шаг перед любой автоматизацией:** проверить IP GitVerse CI runner:

```yaml
# .gitverse/workflows/check-ip.yml
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: curl -s https://ifconfig.me
      - run: curl -s https://www.ozon.ru/search/?text=office | head -c 200
```

Если Ozon отвечает нормально — вся архитектура Codex работает на GitVerse. Если нет — нужен VPS.

---

## Целевая архитектура

```
┌─────────────────────────────────────────────┐
│        СБОР ДАННЫХ (требует RU IP)          │
│                                             │
│  Вариант A: GitVerse CI (cron)              │
│    → если runners имеют российский IP       │
│                                             │
│  Вариант B: Российский VPS                  │
│    → Timeweb/Selectel, ~200-400 ₽/мес       │
│    → только scraping + git push             │
│                                             │
│  Вариант C (текущий): вручную               │
│    → Claude in Chrome на своей машине       │
│    → blob-скачивание → commit               │
└──────────────┬──────────────────────────────┘
               │ git push
       ┌───────┴──────────┐
       ↓                  ↓
  GitVerse repo      GitHub repo (mirror)
       ↓                  ↓
  GitVerse Pages     GitHub Pages
```

---

## Структура репозитория

```
anti-ms-mp/
├── anti-ms-dashboard/          # статический сайт
│   ├── index.html
│   ├── app.js                  # fetch('./data/latest.json') + рендер
│   ├── styles.css
│   └── data/
│       ├── latest.json         # текущие данные (~3000 карточек, ~600KB)
│       ├── meta.json           # дата генерации, счётчики по платформам
│       └── archive/
│           └── YYYY-MM.json    # архив по месяцам (для тренда цен)
├── scraper/
│   ├── scrape-marketplaces.js  # Node.js + Playwright (уже есть)
│   ├── compile_dashboard_json.py  # raw → latest.json (новый, замена compile_mon_raw.py)
│   ├── upload_raw_to_supabase.py  # опционально, для аналитики в dev
│   ├── config/
│   │   ├── queries.json        # список поисковых запросов (легко редактировать)
│   │   └── official_prices.json
│   └── data/
│       ├── raw/
│       │   ├── ozon.txt
│       │   ├── ym.txt
│       │   ├── avito.txt
│       │   ├── wb.txt
│       │   └── aliexpress.txt
│       └── snapshots/
│           └── YYYY-MM-DD/    # архив сырых данных по датам
├── docs/
│   ├── 2026-03-21_Codex_anti-ms-dashboard-architecture.md
│   └── 2026-03-22_Claude_anti-ms-dashboard-architecture.md
├── .gitverse/workflows/
│   └── update-dashboard.yml
└── .github/workflows/
    └── update-dashboard.yml    # mirror push + GitHub Pages
```

---

## Формат latest.json

```json
{
  "generated_at": "2026-03-22T06:00:00Z",
  "schema_version": 1,
  "summary": {
    "items_total": 2432,
    "platforms": {
      "yandex": 1186,
      "ozon": 447,
      "avito": 735,
      "wildberries": 61,
      "aliexpress": 3
    }
  },
  "items": [
    {
      "date": "2026-03-22",
      "pl": "ozon",
      "product_id": "OZ-84120001",
      "query": "Microsoft Office 365 ключ",
      "title": "Office 365 Pro Plus готовая учётная запись",
      "url": "https://www.ozon.ru/product/-3387333078/",
      "price": 115,
      "op": 6990,
      "disc": 98
    }
  ]
}
```

Размер: ~3000 карточек × ~200 байт ≈ **600KB** — нормально для GitHub Pages без CDN.

---

## Динамические запросы (config/queries.json)

```json
{
  "queries": [
    "Microsoft Office ключ активации",
    "Microsoft Office 365 ключ",
    "Office 2021 ключ активации",
    "Office 2024 ключ активации",
    "MS Office ключ активации"
  ],
  "price_range": { "min": 50, "max": 5000 }
}
```

Изменить список запросов = отредактировать один JSON-файл. Scraper читает его при запуске.

---

## CI/CD workflow (GitVerse)

```yaml
# .gitverse/workflows/update-dashboard.yml
name: Update dashboard

on:
  schedule:
    - cron: "0 6 * * *"      # каждый день в 06:00
  workflow_dispatch:          # ручной запуск

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 30

    steps:
      - uses: actions/checkout@v4

      - name: Setup Node 20
        uses: actions/setup-node@v4
        with:
          node-version: "20"

      - name: Install & scrape
        run: |
          cd scraper
          npm ci
          npx playwright install chromium
          node scrape-marketplaces.js
          python3 compile_dashboard_json.py

      - name: Commit if changed
        run: |
          git config user.name "dashboard-bot"
          git config user.email "bot@noreply"
          git add anti-ms-dashboard/data/ scraper/data/raw/ scraper/data/snapshots/
          git diff --cached --quiet || git commit -m "data: update $(date +%Y-%m-%d)"

      - name: Push to GitVerse + GitHub
        run: |
          git push origin main
          git push github main   # настроить remote github заранее
```

---

## Про Supabase в этой архитектуре

**В production pipeline не нужен.** Дашборд читает только `latest.json`.

**Полезен как инструмент разработки:**
- Быстрые SQL-агрегаты при анализе новых данных
- Проверка качества после очередного прогона скрапера
- Запускать опционально: `python3 upload_raw_to_supabase.py`

**Ограничение:** Supabase free tier — 500MB, 2 проекта. При ежедневном обновлении ~3000 записей таблица достигнет лимита за ~6 месяцев (если хранить историю). С upsert без истории — не растёт.

---

## План действий (приоритеты)

### Шаг 1 — немедленно (1 час)
Вынести `MON_RAW` из `index.html` в `data/latest.json` + обновить `app.js` на `fetch()`.
Это независимо от решения про IP и разблокирует всё остальное.

### Шаг 2 — проверить GitVerse CI (30 мин)
Создать тестовый workflow `.gitverse/workflows/check-ip.yml` с одной командой `curl ifconfig.me`.
Результат определяет весь дальнейший путь:
- **RU IP** → настраиваем полный GitVerse CI pipeline, GitHub зеркало
- **не RU IP** → поднимаем Timeweb VPS для scraping

### Шаг 3 — вынести запросы в config/queries.json
Сейчас запросы захардкожены в `scrape-marketplaces.js`. Вынести → можно менять без правки кода.

### Шаг 4 — compile_dashboard_json.py
Новый скрипт: читает raw-файлы → генерирует `anti-ms-dashboard/data/latest.json` в новом формате.
Заменяет `compile_mon_raw.py` (тот патчил HTML напрямую — больше не нужно).

### Шаг 5 — настроить CI pipeline
После Шагов 1-4: один workflow, который делает всё. Daily cron + ручной запуск.

---

## Что НЕ делать

- Не хранить `MON_RAW` внутри `index.html` (уже не делаем после Шага 1)
- Не делать GitHub Actions основным scraper (не-RU IP)
- Не строить hard dependency на Supabase в production
- Не усложнять: VPS нужен только если GitVerse CI не имеет RU IP

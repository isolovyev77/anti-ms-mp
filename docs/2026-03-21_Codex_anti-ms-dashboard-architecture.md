# Архитектура anti-ms-dashboard

## Цель

Зафиксировать рекомендуемую архитектуру для `anti-ms-dashboard` и `scraper/`, при которой:

- дашборд остаётся статическим сайтом;
- данные обновляются автоматически по расписанию;
- логика сбора данных отделена от логики отображения;
- проект можно одинаково публиковать в GitHub, GitVerse, Vercel и на обычном статическом хостинге;
- обновления данных можно отслеживать через git-историю.

## Краткая рекомендация

Предлагаемая модель:

1. `anti-ms-dashboard` содержит только UI и JSON-данные.
2. `scraper/` отвечает за сбор, очистку, нормализацию и компиляцию данных.
3. Cron-задача запускает scraper по расписанию.
4. После успешного обновления создаётся commit с новыми данными.
5. GitHub и GitVerse получают одинаковое обновление через push.
6. Pages/Vercel публикуют уже готовую статику.

Главная идея: не держать большой массив `MON_RAW` внутри `index.html`, а рендерить дашборд из `anti-ms-dashboard/data/latest.json`.

## Почему именно так

Преимущества такой схемы:

- `index.html` почти не меняется;
- данные обновляются независимо от UI;
- уменьшается размер diff при каждом обновлении;
- проще отлаживать scraper;
- проще откатывать неудачные обновления;
- сайт продолжает работать даже если очередной запуск scraper не удался;
- одна и та же архитектура подходит для GitHub Pages, GitVerse Pages, Vercel и VPS.

## Целевая структура файлов

```text
anti-ms-mp/
├── anti-ms-dashboard/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── data/
│       ├── latest.json
│       └── meta.json
├── scraper/
│   ├── package.json
│   ├── README.md
│   ├── SKILL.md
│   ├── config/
│   │   ├── queries.json
│   │   └── official_prices.json
│   ├── src/
│   │   ├── scrape-marketplaces.js
│   │   ├── normalize.js
│   │   └── platforms/
│   │       ├── ozon.js
│   │       ├── wildberries.js
│   │       ├── yandex.js
│   │       ├── avito.js
│   │       └── aliexpress.js
│   ├── data/
│   │   ├── raw/
│   │   │   ├── latest/
│   │   │   │   ├── ozon.txt
│   │   │   │   ├── wb.txt
│   │   │   │   ├── ym.txt
│   │   │   │   ├── avito.txt
│   │   │   │   └── aliexpress.txt
│   │   │   └── snapshots/
│   │   │       └── YYYY-MM-DD/
│   │   └── compiled/
│   │       ├── latest.json
│   │       └── mon_raw_snippet.js
│   └── scripts/
│       ├── compile_dashboard_json.py
│       └── update_dashboard.sh
├── docs/
│   └── anti-ms-dashboard-architecture.md
├── .nojekyll
├── .gitverse/
│   └── workflows/
│       └── update-dashboard.yml
└── .github/
    └── workflows/
        └── update-dashboard.yml
```

## Роли каталогов

### `anti-ms-dashboard/`

Содержит только то, что необходимо браузеру:

- `index.html` — каркас страницы;
- `app.js` — клиентская логика рендера и фильтрации;
- `styles.css` — стили;
- `data/latest.json` — актуальные данные для виджета мониторинга;
- `data/meta.json` — метаданные: дата генерации, платформы, версия схемы, счётчики.

### `scraper/`

Содержит pipeline данных:

- `src/` — runtime-логика скрейпинга;
- `config/` — запросы и справочники;
- `data/raw/latest/` — последние сырые данные по площадкам;
- `data/raw/snapshots/` — исторические снимки;
- `data/compiled/` — промежуточные результаты компиляции;
- `scripts/` — служебные скрипты, которые можно вызывать из cron или CI.

## Текущее состояние и рекомендуемая миграция

Сейчас основная проблема в том, что данные мониторинга встроены прямо в `anti-ms-dashboard/index.html`.

Это создаёт несколько проблем:

- HTML постоянно разрастается;
- каждое обновление даёт большой diff;
- тяжело отделить UI-правки от обновления данных;
- сложно повторно использовать данные в других витринах.

Рекомендуемое изменение:

1. Вынести массив мониторинга из `index.html` в `anti-ms-dashboard/data/latest.json`.
2. Оставить в HTML только логику загрузки JSON.
3. Использовать `scraper/scripts/compile_dashboard_json.py` как официальный этап компиляции.
4. Сохранить `mon_raw_snippet.js` только как временный или отладочный артефакт.

## Рекомендуемый формат `latest.json`

```json
{
  "generated_at": "2026-03-21T12:00:00Z",
  "schema_version": 1,
  "sources": ["ozon", "wildberries", "yandex", "avito", "aliexpress"],
  "summary": {
    "items_total": 245,
    "platforms": {
      "ozon": 41,
      "wildberries": 38,
      "yandex": 96,
      "avito": 52,
      "aliexpress": 18
    }
  },
  "items": [
    {
      "date": "2026-03-21",
      "platform": "ozon",
      "id": "OZ-1234567",
      "query": "Microsoft Office ключ активации",
      "title": "Office 2021 Pro Plus LTSC",
      "url": "https://example.com",
      "price": 199,
      "official_price": 22990,
      "discount_pct": 99,
      "flags": ["F1", "F2", "F6"],
      "reg_days": null,
      "authorized_partner": null
    }
  ]
}
```

## Как должен работать дашборд

Клиентский код должен:

1. загрузить `./data/latest.json` через `fetch()`;
2. отрендерить таблицу и карточки из JSON;
3. отображать метаданные (`generated_at`, количество карточек, платформы);
4. не требовать ручного редактирования HTML при каждом обновлении данных.

### Минимальный принцип

`index.html` не должен знать конкретные данные; он должен знать только схему JSON.

## Как должен работать scraper

Рекомендуемый поток:

1. Скрейпер получает данные с площадок.
2. Каждая площадка пишет результат в `scraper/data/raw/latest/<platform>.txt`.
3. Дополнительно создаётся снапшот в `scraper/data/raw/snapshots/YYYY-MM-DD/`.
4. Компилятор читает raw-файлы, фильтрует записи и генерирует `latest.json`.
5. `anti-ms-dashboard/data/latest.json` обновляется автоматически.
6. Если данные изменились, создаётся commit.

## Рекомендуемый единый entrypoint

Рекомендуется иметь один скрипт для всего pipeline:

`/scraper/scripts/update_dashboard.sh`

Пример:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

npm ci
npx playwright install chromium
node src/scrape-marketplaces.js
python3 scripts/compile_dashboard_json.py
```

Преимущество такого подхода:

- один и тот же entrypoint можно использовать в GitHub Actions, GitVerse CI и VPS cron;
- меньше расхождений между средами;
- проще документировать процесс.

## Как настроить обновление по cron

### Предпочтительный вариант

Использовать CI как scheduler, а не делать VPS основным центром автоматизации.

Предлагаемая схема:

1. GitHub Actions запускается по расписанию.
2. Workflow вызывает `scraper/scripts/update_dashboard.sh`.
3. Если `latest.json` изменился, workflow делает commit.
4. Workflow пушит изменения в GitHub.
5. Тем же шагом workflow зеркалит изменения в GitVerse.
6. Vercel обновляется от GitHub, GitVerse Pages обновляется от GitVerse.

### Почему это предпочтительно

- не нужен постоянно обслуживаемый VPS;
- scheduler переносим между платформами;
- статический сайт легче поддерживать;
- история обновлений хранится прямо в git.

## GitVerse cron workflow

GitVerse поддерживает `schedule` в workflow. Файлы workflow лежат в `.gitverse/workflows/`.

Пример файла `.gitverse/workflows/update-dashboard.yml`:

```yaml
name: Update dashboard

on:
  schedule:
    - cron: "0 */6 * * *"
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - uses: actions/checkout@v4

      - name: Setup Node
        uses: actions/setup-node@v4
        with:
          node-version: "20"

      - name: Install dependencies
        run: |
          cd scraper
          npm ci
          npx playwright install chromium

      - name: Update dashboard data
        run: |
          cd scraper
          ./scripts/update_dashboard.sh

      - name: Commit changes
        run: |
          git config user.name "dashboard-bot"
          git config user.email "dashboard-bot@example.local"
          git add anti-ms-dashboard/data/latest.json scraper/data/raw/latest scraper/data/raw/snapshots
          git diff --cached --quiet || git commit -m "Update dashboard data"

      - name: Push changes
        run: git push
```

## GitHub workflow

Если GitHub выступает как основной источник автоматизации, аналогичный workflow хранится в `.github/workflows/update-dashboard.yml`.

Практически рекомендуется:

- GitHub использовать как primary scheduler;
- GitVerse использовать как mirror-remote для Pages.

## Когда нужен VPS

VPS нужен только если возникает хотя бы одно из условий:

- требуется постоянно работающий backend;
- scraping должен идти чаще и тяжелее, чем удобно в CI;
- нужны прокси, очереди, браузеры 24/7 и сложная антибот-логика;
- нужен on-demand API, а не только периодическое обновление данных.

Для текущего проекта VPS не обязателен.

## Что бы я сделал на практике

### Этап 1

Минимальный рефакторинг без переписывания проекта:

1. оставить текущий UI;
2. вынести `MON_RAW` из `anti-ms-dashboard/index.html` в `anti-ms-dashboard/data/latest.json`;
3. обновить `compile_mon_raw.py`, чтобы он писал JSON;
4. подключить JSON в `app.js`.

### Этап 2

Нормализация структуры scraper:

1. перенести raw-файлы в `scraper/data/raw/latest/`;
2. разнести платформы по `scraper/src/platforms/`;
3. вынести запросы и цены в `scraper/config/`.

### Этап 3

Автоматизация:

1. добавить `update_dashboard.sh`;
2. подключить cron workflow;
3. пушить обновления в GitHub и GitVerse автоматически.

## Что не рекомендую

Не рекомендую держать как основной подход:

- ручное копирование сниппета в HTML;
- giant inline dataset внутри `index.html`;
- VPS как первую и обязательную среду;
- сильную зависимость от одной платформы деплоя.

## Итог

Целевая архитектура должна быть такой:

- `anti-ms-dashboard` = статический UI + JSON;
- `scraper/` = pipeline сбора и компиляции данных;
- cron = автоматическое обновление данных;
- git = история, контроль и доставка обновлений;
- GitHub и GitVerse = каналы публикации одной и той же статики.

Это наиболее простой, переносимый и поддерживаемый вариант для текущего проекта.

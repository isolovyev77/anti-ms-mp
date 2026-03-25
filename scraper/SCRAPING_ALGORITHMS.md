# Алгоритмы парсинга маркетплейсов
## Для деплоя на VPS с кронами

**Проект:** anti-ms-mp dashboard
**Цель:** сбор листингов нелицензионных ключей Microsoft Office на OZON, WB, Avito, Яндекс Маркет, AliExpress
**Формат выходного файла:** `price|cardId|query|title` (4 колонки, UTF-8)

---

## Общая архитектура

```
cron → scraper_ozon.py (или .js headless) → ozon_raw.txt
                                               ↓
cron → scraper_wb.py                 → wb_raw.txt
cron → scraper_avito.py              → avito_raw.txt     → compile_mon_raw.py → index.html
cron → scraper_ym.py                 → ym_raw.txt
cron → scraper_ale.py  (headless)    → ale_raw.txt
                                               ↑
                              (запускается после всех scrapers)
```

Рекомендуемая последовательность в cron:
```
0 6 * * * /path/scraper_ozon.py >> /var/log/anti-ms.log 2>&1
15 6 * * * /path/scraper_wb.py >> /var/log/anti-ms.log 2>&1
30 6 * * * /path/scraper_avito.py >> /var/log/anti-ms.log 2>&1
45 6 * * * /path/scraper_ym.py >> /var/log/anti-ms.log 2>&1
50 6 * * * /path/scraper_ale.py >> /var/log/anti-ms.log 2>&1
0 7 * * * /path/compile_mon_raw.py >> /var/log/anti-ms.log 2>&1
```

---

## Формат raw-файлов

Каждый файл (`ozon_raw.txt`, `wb_raw.txt`, `avito_raw.txt`, `ym_raw.txt`, `ale_raw.txt`) - текстовый, UTF-8, одна запись на строку:

```
217|2926983276|Microsoft Office ключ активации|Microsoft Office 2024 Pro Plus LTSC Ключ активации
```

Поля (разделитель `|`):
1. `price` - цена в рублях (целое число)
2. `cardId` - уникальный ID карточки на платформе (см. ниже для каждой)
3. `query` - поисковый запрос, которым найдена карточка
4. `title` - заголовок листинга (до 120 символов)

**compile_mon_raw.py** при обработке:
- фильтрует по цене: 50-5000 руб. (дороже 5000 - не контрафактные ключи)
- фильтрует по title через `title_ok()` (см. раздел ниже)
- дедуплицирует по cardId
- определяет официальную цену (`op`) по title для расчёта скидки

---

## title_ok() - фильтр по заголовку

**Актуальная версия** (важно: предыдущие широкие версии захватывали мусор):

```python
def title_ok(title: str) -> bool:
    t = title.lower()
    has_ms_office = (
        'office' in t or
        ('365' in t and 'microsoft' in t) or
        ('офис' in t and 'microsoft' in t)
    )
    return has_ms_office
```

**Почему строго**: широкие фильтры с 'microsoft', 'лиценз', '2021' и т.д. захватывают:
- ключи активации Windows (не Office)
- Celemony Melodyne (содержит "лицензионный")
- МойОфис (содержит "офис", но не Microsoft)
- другое несвязанное ПО

Всегда требовать явного упоминания `office` или комбинации `microsoft` + `365`/`офис`.

---

## OZON

### Поисковые запросы
```python
QUERIES = [
    'Microsoft Office ключ активации',
    'Microsoft Office 365 ключ',
    'Microsoft Office 2021 ключ',
    'Microsoft Office 2024 ключ',
]
```

### URL пагинации
```
https://www.ozon.ru/search/?text=Microsoft+Office+%D0%BA%D0%BB%D1%8E%D1%87+%D0%B0%D0%BA%D1%82%D0%B8%D0%B2%D0%B0%D1%86%D0%B8%D0%B8&page=1
```
- Параметр `page=N`, начиная с 1
- Обычно 18-25 страниц значимых результатов
- ~25 карточек на страницу
- Страница без карточек = конец результатов

### Извлечение cardId
URL карточки: `https://www.ozon.ru/product/microsoft-office-2024-pro-plus-2926983276/`
cardId = последний числовой сегмент (5-12 цифр) перед `/`:
```python
import re
match = re.search(r'-(\d{5,12})/$', product_url)
card_id = match.group(1) if match else None
```

### DOM-структура (для headless/selenium/playwright)
```
div[data-widget="searchResultsV2"]         # обёртка результатов
  └─ div.tile-root / div[data-index]        # карточка
       ├─ a[href*="/product/"]              # ссылка (содержит cardId в URL)
       ├─ span[class*="tsBody500Medium"]    # цена (несколько элементов)
       └─ span[class*="tsBody400Small"]     # заголовок
```

### Алгоритм скрапинга (псевдокод Python + Playwright)
```python
from playwright.sync_api import sync_playwright
import re

def scrape_ozon(query: str, max_pages: int = 20) -> list[dict]:
    results = []
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...',
            locale='ru-RU',
        )
        page = context.new_page()

        for page_num in range(1, max_pages + 1):
            url = f'https://www.ozon.ru/search/?text={quote(query)}&page={page_num}'
            page.goto(url, wait_until='networkidle', timeout=30000)

            # Прокрутка для загрузки lazy-load карточек
            for i in range(1, 13):
                page.evaluate(f'window.scrollTo(0, document.body.scrollHeight * {i}/12)')
                page.wait_for_timeout(400)

            # Сбор карточек
            cards_data = page.evaluate('''() => {
                const results = [];
                const links = document.querySelectorAll('a[href*="/product/"]');
                const seenPaths = new Set();
                for (const a of links) {
                    const m = a.href.match(/-(\d{5,12})\/$/);
                    if (!m) continue;
                    const cardId = m[1];
                    if (seenPaths.has(cardId)) continue;
                    seenPaths.add(cardId);

                    const samePath = document.querySelectorAll(`a[href="${a.getAttribute('href')}"]`);
                    let title = '';
                    for (const el of samePath) {
                        const t = el.innerText.trim().replace(/\\n/g,' ').slice(0,120);
                        if (t.length > title.length) title = t;
                    }
                    if (title.length < 10) continue;

                    const container = a.closest('div[data-index], div.tile-root') || a.parentElement;
                    const text = container ? container.innerText : '';
                    const priceMatch = text.match(/(\d[\d\\s]{1,5})\\s*₽/);
                    if (!priceMatch) continue;
                    const price = parseInt(priceMatch[1].replace(/\\s/g,''));
                    if (!price || price < 50 || price > 10000) continue;

                    results.push({cardId, title, price});
                }
                return results;
            }''')

            if not cards_data:
                break

            for item in cards_data:
                if item['cardId'] not in seen_ids:
                    seen_ids.add(item['cardId'])
                    results.append({
                        'price': item['price'],
                        'cardId': item['cardId'],
                        'query': query,
                        'title': item['title'],
                    })

        browser.close()
    return results
```

### Особенности и антиблокировка

**ВАЖНОЕ ОБНОВЛЕНИЕ (март 2026):** Playwright headless **работает на Ozon** при запуске с локальной машины с российским IP. Ключевые условия:

1. **Флаги запуска браузера** — обязательны:
   ```javascript
   chromium.launch({
     headless: true,
     args: [
       '--no-sandbox',
       '--disable-setuid-sandbox',
       '--disable-blink-features=AutomationControlled',  // КРИТИЧНО
       '--lang=ru-RU',
     ]
   })
   ```
2. **Контекст с русской локалью** — Ozon проверяет заголовки:
   ```javascript
   browser.newContext({
     userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
     locale: 'ru-RU',
     timezoneId: 'Europe/Moscow',
     viewport: { width: 1366, height: 768 },
     extraHTTPHeaders: { 'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8' },
   })
   ```
3. **Скрыть navigator.webdriver** через initScript:
   ```javascript
   context.addInitScript(() => {
     Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
     window.chrome = { runtime: {} };
   });
   ```
4. **Российский IP** — обязателен. С американских/европейских IP (Firecrawl, VPS без VPN) Ozon возвращает 403. Datacenter IP блокируется. Решение: запускать с локальной машины пользователя.
5. **Lazy-load прокрутка** обязательна: карточки подгружаются при скролле до конца страницы.

Результат (22 марта 2026): 1 243 карточки за один прогон (5 запросов × 10 страниц).

---

## Wildberries

### Поисковые запросы
WB возвращает максимум ~15 результатов на запрос в категории software. Для полного охвата нужно несколько запросов:
```python
QUERIES = [
    'Microsoft Office ключ активации',
    'Microsoft Office 365 ключ',
    'Microsoft Office 2021 ключ',
    'Microsoft Office 2024 ключ',
    'Microsoft Office 2019 ключ',
    'Windows лицензионный ключ',
]
```

### URL пагинации
```
https://www.wildberries.ru/catalog/0/search.aspx?search=Microsoft+Office+ключ+активации
```
- WB для категории "программы/ключи" обычно показывает только 1 страницу (~15 результатов)
- `?page=2` часто редиректит обратно на `?page=1` - не использовать пагинацию
- Вместо пагинации - несколько разных запросов

### API-подход (рекомендуется для WB)
WB имеет внутренний JSON API, который используется сайтом:
```
GET https://search.wb.ru/exactmatch/ru/common/v9/search?
    appType=1&curr=rub&dest=-1257786&lang=ru
    &query=Microsoft+Office+ключ+активации
    &resultset=catalog&sort=popular&spp=30
    &suppressSpellcheck=false
    &page=1
```

Ответ содержит `data.products[]` с полями:
- `id` - nmId (используется как cardId)
- `name` - название (можно использовать как title)
- `priceU` - цена в копейках (делить на 100)

```python
import requests

def scrape_wb_api(query: str, max_pages: int = 5) -> list[dict]:
    results = []
    seen_ids = set()

    for page_num in range(1, max_pages + 1):
        url = 'https://search.wb.ru/exactmatch/ru/common/v9/search'
        params = {
            'appType': 1, 'curr': 'rub', 'dest': -1257786,
            'lang': 'ru', 'query': query,
            'resultset': 'catalog', 'sort': 'popular', 'spp': 30,
            'suppressSpellcheck': 'false', 'page': page_num,
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 ...',
            'Accept': '*/*',
            'Origin': 'https://www.wildberries.ru',
            'Referer': 'https://www.wildberries.ru/',
        }

        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()

        products = data.get('data', {}).get('products', [])
        if not products:
            break

        for p in products:
            nm_id = str(p.get('id', ''))
            if not nm_id or nm_id in seen_ids:
                continue
            seen_ids.add(nm_id)

            price_raw = p.get('priceU', 0)
            price = price_raw // 100
            if not price or price < 50 or price > 10000:
                continue

            title = p.get('name', '').strip()
            brand = p.get('brand', '').strip()
            if brand and brand not in title:
                title = f'{brand} / {title}'
            title = title[:120]

            results.append({
                'price': price,
                'cardId': nm_id,
                'query': query,
                'title': title,
            })

        if len(products) < 10:
            break

    return results
```

### Особенности
- Lazy-load обязателен при DOM-скрапинге
- API более стабилен чем DOM, но может менять версию (`v9` → `v10`)
- `dest=-1257786` - регион Москва, влияет на выдачу и цены
- Ответ API не требует авторизации, но может ввести rate limiting

---

## Avito

### Поисковые запросы
```python
QUERIES = [
    'Microsoft Office ключ активации',
    'ключ активации office',
    'Microsoft Windows лицензия',
]
```

### URL пагинации
```
https://www.avito.ru/all/igry_pristavki_i_programmy/programmy-ASgBAgICAUSSAs4J
    ?q=Microsoft+Office+ключ+активации&p=1
```
- Параметр `&p=N` (начиная с 1)
- ~38-40 карточек на страницу
- Максимум 15 страниц (p=16 редиректит на p=1)
- Страница без новых карточек = конец

### Извлечение cardId
cardId = атрибут `data-item-id` на карточке:
```python
item_id = card.get_attribute('data-item-id')
```

URL карточки: `https://www.avito.ru/items/{item_id}`

### DOM-структура
```
[data-item-id]                              # карточка (data-item-id = cardId)
  ├─ [itemprop="name"] / h3                 # заголовок
  ├─ [data-marker="item-title"] a           # ссылка на объявление
  └─ [class*="price-root"] / [class*="Price"]  # цена
```

### Алгоритм (Python + Playwright)
```python
def scrape_avito(query: str, max_pages: int = 10) -> list[dict]:
    results = []
    seen_ids = set()
    base_url = 'https://www.avito.ru/all/igry_pristavki_i_programmy/programmy-ASgBAgICAUSSAs4J'

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 ...',
            locale='ru-RU',
            geolocation={'latitude': 55.7558, 'longitude': 37.6173},  # Москва
        )
        page = context.new_page()

        for page_num in range(1, max_pages + 1):
            url = f'{base_url}?q={quote(query)}&p={page_num}'
            page.goto(url, wait_until='domcontentloaded', timeout=30000)

            for i in range(1, 10):
                page.evaluate(f'window.scrollTo(0, document.body.scrollHeight * {i}/8)')
                page.wait_for_timeout(500)

            cards_data = page.evaluate('''() => {
                const results = [];
                const cards = document.querySelectorAll('[data-item-id]');
                for (const card of cards) {
                    const itemId = card.getAttribute('data-item-id');
                    if (!itemId) continue;
                    const titleEl = card.querySelector('[itemprop="name"], h3');
                    const title = titleEl
                        ? titleEl.innerText.trim().replace(/\\n/g,' ').slice(0,120)
                        : '';
                    if (title.length < 5) continue;
                    const priceEl = card.querySelector('[class*="price-root"], [class*="Price"]');
                    if (!priceEl) continue;
                    const priceM = priceEl.innerText.match(/(\\d[\\d\\s]*)/);
                    if (!priceM) continue;
                    const price = parseInt(priceM[1].replace(/\\s/g,''));
                    if (!price || price < 50 || price > 10000) continue;
                    results.push({itemId, title, price});
                }
                return results;
            }''')

            if not cards_data:
                break

            added = 0
            for item in cards_data:
                if item['itemId'] not in seen_ids:
                    seen_ids.add(item['itemId'])
                    results.append({
                        'price': item['price'],
                        'cardId': item['itemId'],
                        'query': query,
                        'title': item['title'],
                    })
                    added += 1

            if added == 0:
                break

        browser.close()
    return results
```

### Особенности и антиблокировка
- Avito активно блокирует ботов: капча, JS-fingerprinting
- Рекомендуется: `playwright-stealth`, residential proxy, имитация человеческого поведения
- Случайные задержки 3-7 сек между страницами
- Альтернатива: Avito API (платный, `api.avito.ru`) - для партнёров

---

## Яндекс Маркет

### Поисковые запросы
```python
QUERIES = [
    'Microsoft Office ключ активации',
    'Office 365 ключ',
    'Office 2021 ключ',
]
```

### URL пагинации
```
https://market.yandex.ru/search?text=Microsoft+Office+ключ+активации&page=1
```
- Параметр `page=N`
- ~12-24 карточек на страницу
- Страница 20 = конец (редирект на главную)

### Альтернативный API (рекомендуется)
Яндекс Маркет имеет официальный партнёрский API:
`https://api.content.market.yandex.ru/v2/search.json?query=...`
Требует API-ключ партнёра.

### DOM-структура (без API)
```
article[data-autotest-id="product-snippet"]   # карточка
  ├─ a[href*="/product/"]                      # ссылка (содержит productId)
  ├─ h3 / [data-auto="snippet-title"]          # заголовок
  └─ [data-auto="snippet-price-current"]       # цена
```

cardId из URL: `/product/microsoft-office--1234567/` → `1234567`
Или из `data-sku-id` атрибута.

### Особенности
- ЯМ требует авторизации для некоторых запросов при массовом парсинге
- Использует React/Next.js, большая часть данных в `__NEXT_DATA__` скрипте
- Альтернатива: читать JSON из `<script id="__NEXT_DATA__">`:
```python
import json, re
match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
data = json.loads(match.group(1))
```

---

## AliExpress Russia

### Статус площадки
AliExpress Russia имеет крайне малый пул товаров Office - ~5 уникальных позиций по всем запросам.
Страницы 2+ дают 0 новых результатов. Автоматизация менее приоритетна.

### Поисковые запросы
```python
QUERIES = [
    'Office 2021 ключ активации',
    'Office 365 ключ активации',
    'Microsoft Office ключ',
    'Office 2024 ключ',
]
```

### URL пагинации
```
https://aliexpress.ru/wholesale?SearchText=ENCODED_QUERY&page=N
```
- Параметр `page=N`, начиная с 1
- Признак конца: менее 3 карточек ИЛИ 0 новых items 2 страницы подряд

### DOM-структура
```
[data-product-id]                - карточка товара
  ├─ data-product-id             - ненадёжен (часто "1"), не использовать
  ├─ a[href*="/item/"]           - href содержит настоящий itemId
  └─ innerText                   - цена (ВНИМАНИЕ: NBSP U+A0 как разделитель тысяч)
```

### КРИТИЧЕСКИЙ БАГ: NBSP в ценах

AliExpress использует **неразрывный пробел U+A0** (NBSP) как разделитель тысяч.
Обычный `\s` или `\d[\d ]` его не матчит.

```python
# В JavaScript:
const rawText = c.innerText.replace(/\u00a0/g, ' ');

# В Python:
price_text = price_text.replace('\u00a0', ' ')
```

### КРИТИЧЕСКИЙ БАГ: cardId = "1"

`data-product-id` у большинства карточек = `"1"`. Настоящий ID только в href:

```python
# В JavaScript:
const link = c.querySelector('a[href*="/item/"]');
const itemId = link ? (link.href.match(/\/item\/(\d+)/) || [])[1] : null;

# В Python (BeautifulSoup/Playwright):
link = card.query_selector('a[href*="/item/"]')
m = re.search(r'/item/(\d+)', link.get_attribute('href'))
item_id = m.group(1) if m else None
```

Для карточек где itemId не извлёкся - назначить синтетический ID вместо пропуска:
```python
if not item_id or len(item_id) < 5:
    item_id = f'10050000{counter:05d}'
    counter += 1
```

### URL карточек AliExpress
```
https://aliexpress.ru/item/{itemId}.html
```

---

## Проверенные, но отклонённые площадки

### Сбер МегаМаркет
- **Проверен** (март 2026): только 1 легитимный Office 2019 по ~33 000 ₽
- Подозрительных дешёвых ключей нет - добавлять в мониторинг нецелесообразно
- Техническое примечание: сайт показывает "Сайт больше не поддерживает ваш браузер" но результаты загружаются
- Запрос "ключ" выдаёт физические ключи-гаечники (Deli и т.д.) - нужен точный запрос "Microsoft Office ключ активации"

### ВКонтакте Маркет
- Публичный поиск требует авторизации - массовый парсинг невозможен
- Механика продаж через ЛС группы, не через корзину - нет структурированных данных
- Продают **аккаунты** (логин+пароль), а не ключи активации - другая схема мошенничества
- VK Маркет как агрегатор показывает товары OZON/WB (бейджи "ЗАКАЗ НА OZON") - дубли уже покрыты
- Вывод: не добавляем

---

## compile_mon_raw.py - обновление даты

При запуске на VPS дата должна браться динамически:

```python
# Заменить статичную строку:
# today = datetime.strptime('2026-03-19', '%Y-%m-%d')

# На динамическую:
today = datetime.today()
```

---

## Итоговый pipeline на Python

```python
#!/usr/bin/env python3
# run_all_scrapers.py - запускать из cron

import subprocess
import sys
from pathlib import Path

SCRAPER_DIR = Path('/path/to/scraper')

scrapers = [
    'scraper_ozon.py',
    'scraper_wb.py',
    'scraper_avito.py',
    'scraper_ym.py',
    'scraper_ale.py',
]

for scraper in scrapers:
    path = SCRAPER_DIR / scraper
    if not path.exists():
        print(f'SKIP: {scraper} not found')
        continue
    print(f'Running {scraper}...')
    result = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f'ERROR in {scraper}:', result.stderr)

# Компиляция дашборда
compile_path = SCRAPER_DIR / 'compile_mon_raw.py'
result = subprocess.run([sys.executable, str(compile_path)], capture_output=True, text=True)
print(result.stdout)
```

Cron-запись (ежедневно в 6:00):
```
0 6 * * * /usr/bin/python3 /path/to/run_all_scrapers.py >> /var/log/anti-ms-scraper.log 2>&1
```

---

## Node.js Playwright scraper (рекомендуемый подход)

Файл `scrape-marketplaces.js` — основной автоматический скрапер. Запускать с локальной машины.

### Запуск
```bash
cd scraper
node scrape-marketplaces.js                             # все площадки, 5 запросов, 5 страниц
node scrape-marketplaces.js --platforms ozon --pages 10 # только Ozon, 10 страниц
node scrape-marketplaces.js --query "Office 365 ключ"   # один запрос
```

### Установка Chromium (один раз)
```bash
cd scraper
npm install
npx playwright install chromium
# На VPS дополнительно:
npx playwright install-deps chromium
```

Chromium хранится в `~/.cache/ms-playwright/chromium-XXXX/` — повторно не скачивается.

### Вывод
- `mon_data.json` — полный JSON `{ generated, queries, total, stats, data[] }`
- `mon_raw_snippet.js` — JS-массив `MON_RAW` для вставки в дашборд

### Формат записи в mon_data.json
```json
{
  "date": "2026-03-22",
  "pl": "ozon",
  "id": "OZ-84120001",           // НЕ совпадает с cardId из raw-файлов!
  "query": "Microsoft Office 365 ключ",
  "title": "Office 365 Pro Plus...",
  "url": "https://www.ozon.ru/...",
  "price": 157,
  "op": 6990,
  "regDays": null,
  "auth": null,
  "flags": { "disc": 97, "F1": true, "F2": true, ... }
}
```

**Важно про product_id:** скрапер генерирует id как `{PREFIX}-{qhash}{idx}`, а НЕ через реальный cardId. Для Supabase upsert это нормально, но при повторных запусках одни и те же карточки получат разные id.

### Известный баг: `*/` в JS-комментарии
Cron-строка `*/6` внутри блочного комментария `/** ... */` ломает парсинг — `*/` завершает комментарий досрочно. Решение: экранировать в комментарии как `*\/6` или выносить cron-пример за пределы блочного комментария.

---

## Supabase upload pipeline

После получения данных (из `mon_data.json` или raw-файлов) — загрузка в Supabase через REST API.

### Скрипт upload_raw_to_supabase.py
Загружает данные из raw-файлов (ym_raw.txt, avito_raw.txt, wb_raw.txt, ale_raw.txt) в таблицу `listings`:
```bash
python3 upload_raw_to_supabase.py
```

### Почему curl, а не Python requests
На macOS Python `urllib` / `requests` выдают SSL ошибку при обращении к Supabase:
```
SSL: CERTIFICATE_VERIFY_FAILED
```
Решение — subprocess + `curl`. Работает стабильно:
```python
subprocess.run(['curl', '-s', '-X', 'POST', f'{SUPABASE_URL}/rest/v1/listings',
    '-H', f'apikey: {KEY}', '-H', 'Prefer: resolution=merge-duplicates',
    '-d', json.dumps(batch)])
```

### Upsert по product_id
Supabase REST API поддерживает upsert через заголовок `Prefer: resolution=merge-duplicates`.
Таблица `listings` имеет `UNIQUE` constraint на `product_id` — повторный запуск обновляет существующие записи, не дублирует.

### product_id форматы
| Источник | Формат | Пример |
|---------|--------|--------|
| `ozon_raw.txt` / `scrape-marketplaces.js` | `OZ-{qhash}{idx}` | `OZ-84120001` |
| `ym_raw.txt` | `YM-{cardId}` | `YM-xep3ubtl9ng` |
| `avito_raw.txt` | `AV-{cardId}` | `AV-4294363995` |
| `wb_raw.txt` | `WB-{cardId}` | `WB-130635707` |
| `ale_raw.txt` | `AL-{cardId}` | `AL-1005000000000` |

### Статистика (22 марта 2026)
| Площадка | Записей | Мин. цена | Средняя |
|----------|---------|-----------|---------|
| yandex   | 1 186   | 50 ₽      | 605 ₽   |
| ozon     | 1 038   | 110 ₽     | 389 ₽   |
| avito    | 735     | 50 ₽      | 821 ₽   |
| wildberries | 61   | 107 ₽    | 996 ₽   |
| aliexpress | 3     | 3 451 ₽  | 3 766 ₽ |
| **ИТОГО** | **3 023** |         |         |

---

## Зависимости

```
playwright>=1.40.0
playwright-stealth>=0.0.1   # опционально, для антиблокировки
requests>=2.31.0
```

Установка Playwright:
```bash
pip install playwright playwright-stealth
playwright install chromium
```


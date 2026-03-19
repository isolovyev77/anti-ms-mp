# Алгоритмы парсинга маркетплейсов
## Для деплоя на VPS с кронами

**Проект:** anti-ms-mp dashboard
**Цель:** сбор листингов нелицензионных ключей Microsoft Office на OZON, WB, Avito, Яндекс Маркет
**Формат выходного файла:** `price|cardId|query|title` (4 колонки, UTF-8)

---

## Общая архитектура

```
cron → scraper_ozon.py (или .js headless) → ozon_raw.txt
                                               ↓
cron → scraper_wb.py                 → wb_raw.txt
cron → scraper_avito.py              → avito_raw.txt     → compile_mon_raw.py → index.html
cron → scraper_ym.py                 → ym_raw.txt
                                               ↑
                              (запускается после всех scrapers)
```

Рекомендуемая последовательность в cron:
```
0 6 * * * /path/scraper_ozon.py >> /var/log/anti-ms.log 2>&1
15 6 * * * /path/scraper_wb.py >> /var/log/anti-ms.log 2>&1
30 6 * * * /path/scraper_avito.py >> /var/log/anti-ms.log 2>&1
45 6 * * * /path/scraper_ym.py >> /var/log/anti-ms.log 2>&1
0 7 * * * /path/compile_mon_raw.py >> /var/log/anti-ms.log 2>&1
```

---

## Формат raw-файлов

Каждый файл (`ozon_raw.txt`, `wb_raw.txt`, `avito_raw.txt`, `ym_raw.txt`) - текстовый, UTF-8, одна запись на строку:

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
- фильтрует по title: должно содержать office/microsoft/365/2021/2024/2019/2016/2013/ключ актив/лиценз
- дедуплицирует по cardId (если есть) или по title.lower()
- определяет официальную цену (`op`) по title для расчёта скидки

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

                    // Заголовок - самый длинный текст среди ссылок с таким же href
                    const samePath = document.querySelectorAll(`a[href="${a.getAttribute('href')}"]`);
                    let title = '';
                    for (const el of samePath) {
                        const t = el.innerText.trim().replace(/\\n/g,' ').slice(0,120);
                        if (t.length > title.length) title = t;
                    }
                    if (title.length < 10) continue;

                    // Цена - ищем в родительском блоке
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
                break  # конец результатов

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
- OZON активно блокирует headless-браузеры. Рекомендуется:
  - `playwright-stealth` или `undetected-playwright`
  - Случайные задержки между страницами (2-5 сек)
  - Ротация user-agent
  - Использование residential proxy (datacenter IP блокируется быстро)
- Альтернатива: OZON Data API (если доступен для партнёров)
- Lazy-load обязателен: карточки подгружаются при скролле

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
            break  # мало результатов - конец

    return results
```

### DOM-подход (если API изменится)
```
.product-card                              # карточка
  ├─ a[href*="/catalog/"][href*="/detail.aspx"]  # ссылка → nmId в URL
  ├─ .product-card__brand-wrap (H2)        # заголовок
  └─ .price__lower-price                  # цена
```

cardId из URL: `/catalog/130635707/detail.aspx` → `130635707`

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
- Обычно 5-10 значимых страниц
- Страница без карточек = конец

### Извлечение cardId
cardId = атрибут `data-item-id` на карточке:
```python
# Playwright/BeautifulSoup
item_id = card.get_attribute('data-item-id')
# или
item_id = card['data-item-id']
```

URL карточки: `https://www.avito.ru/items/{item_id}`
Полный URL также доступен из `a[data-marker="item-title"]`

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

            # Скролл для lazy-load
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
                break  # все карточки уже видели

        browser.close()
    return results
```

### Особенности и антиблокировка
- Avito активно блокирует ботов: капча, JS-fingerprinting
- Рекомендуется: `playwright-stealth`, residential proxy, имитация человеческого поведения
- Случайные задержки 3-7 сек между страницами
- Avito может потребовать решения captcha (CAPTCHA solver или ручное вмешательство)
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
# обходить data['props']['pageProps'] для листингов
```

---

## compile_mon_raw.py - обновление даты

При запуске на VPS дата должна браться динамически:

```python
# Заменить статичную строку:
# today = datetime.strptime('2026-03-19', '%Y-%m-%d')

# На динамическую:
today = datetime.today()
```

Также рекомендуется сохранять дату парсинга в raw-файлах или в отдельном метафайле для отслеживания свежести данных.

---

## Итоговый pipeline на Python

```python
#!/usr/bin/env python3
# run_all_scrapers.py - запускать из cron

import subprocess
import sys
from pathlib import Path

SCRAPER_DIR = Path('/path/to/scraper')
LOG = SCRAPER_DIR / 'scraper.log'

scrapers = [
    'scraper_ozon.py',
    'scraper_wb.py',
    'scraper_avito.py',
    'scraper_ym.py',
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
compile_path = SCRAPER_DIR.parent / 'compile_mon_raw.py'
result = subprocess.run([sys.executable, str(compile_path)], capture_output=True, text=True)
print(result.stdout)
```

Cron-запись (ежедневно в 6:00):
```
0 6 * * * /usr/bin/python3 /path/to/run_all_scrapers.py >> /var/log/anti-ms-scraper.log 2>&1
```

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

---

## Итоговые объёмы (по состоянию на 2026-03-19)

| Площадка | Raw строк | После фильтра | В дашборде |
|----------|-----------|---------------|------------|
| OZON     | 349       | 160           | 160        |
| WB       | 72        | 62            | 62         |
| Avito    | 265       | 256           | 256        |
| YM       | 361       | 358           | 358        |
| **Итого**| **1047**  | **836**       | **836**    |

Фильтр отсекает: цена вне диапазона 50-5000 руб. + не связанные с Office/Microsoft заголовки.

# Логика дашборда мониторинга нелицензионного ПО

Документ описывает **всю логику** текущей страницы `anti-ms-dashboard/index.html`
(раздел «Мониторинг»). Назначение — дать соседней сессии Claude Code,
которая делает новую версию `index_new.html` на Supabase, полный референс,
чтобы повторить все настройки и алгоритмы. Файл должен поддерживаться
актуальным при любых изменениях в логике.

Версия документа: **2026-05-27**.
Последний коммит, синхронный с этим документом: см. `git log --oneline | head -5`.

---

## ⚠️ TL;DR — критические правила для index_new.html

> Этот блок — sticky-сводка для соседней сессии. **Прочитать ДО начала
> рефакторинга**. Каждое правило — следствие конкретного бага, на котором
> уже наступали. Подробности — в соответствующих разделах ниже.

### 🔑 Дедупликация
- Ключ дедупа: `(pl, id)` где `id = '{ПРЕФИКС}-{native_cardId}'`
- Префиксы: `OZ-` Ozon, `AV-` Avito, `WB-` Wildberries, `YM-` Yandex.Market
- Дедупликация **обязательна между разными query** — одна и та же карточка
  попадает в выдачу по 3-5 разным поисковым запросам. Без дедупа Я.Маркет
  показывал 1186 «карточек» при ~150 реальных уникальных.

### 💰 Парсинг цены (баги, которые УЖЕ исправлены)
1. **NBSP-разделитель тысяч на WB и ЯМ** — `2 021 ₽` разделено U+00A0 (NBSP),
   а не обычным пробелом U+20. Регекс `[\d ]{2,7}` ловит **только U+20** и
   достаёт только `021` → парсер пишет `21` вместо `2021`. Фикс:
   `text.replace(/[\s   ]+/g, ' ')` перед regex.
2. **Кешбэк на Ozon** ловится как основная цена — рядом с ценой стоит
   «Кешбэк 49 ₽» (отдельный span с ₽). Старая логика брала `min(prices)` —
   получали кешбэк. Фикс: `min(prices, где price >= 100)`.
3. **Lazy-load JS-карточки на ЯМ** — до прокрутки `article[data-auto="searchOrganic"]`
   содержит JS-код apiary с числами типа `serpEntity17798...20` (regex
   ложно матчит «20»). Фильтр: пропустить карточку если `innerText`
   начинается с `(window.|apiary|^{"widgets"`.
4. **YM dynamic pricing** (НЕ баг, **оставляем как есть**): в search-выдаче
   ЯМ показывает цену 32 ₽, на странице товара 28 ₽ (скидка через Я.Пэй).
   Парсер берёт search-цену — это то, что видит покупатель ПЕРВЫМ. Не
   меняем — корректно для мониторинга.

### 🚫 Что НЕ контрафакт
- Карточки с `price >= 0.5 × op` отбрасываются на этапе импорта
  (`scraper/import_cloak_full.py:normalize_item`). Эта эвристика убирает
  легальные товары со скидкой <50% и карточки с **завышенной** ценой
  (Office Pro Plus за 59 680 ₽ при op=14990 — это бизнес-лицензия, не
  контрафакт).

### 🏷️ Официальная цена Microsoft (8 типов)
Определяется по ключевым словам в `title` (см. `official_price()`):

| Тип | op (₽) | Триггер в title |
|---|---:|---|
| `office_pro` (B2B) | **39 990** | `pro plus`, `professional plus`, `pro+`, `ltsc` |
| `office_home_bus` | 22 990 | `home and business`, `для работы`, ` h&b` |
| `office_home_student` | 14 990 | `home and student`, `для дома и учёбы`, либо просто `2024/2021/2019/2016` без редакции |
| `m365_family` | 5 290 | `family`, `семь`, `для семьи` |
| `m365_personal` | 2 790 | `personal`, `персональн`, `m365`, `1тб onedrive` |
| `office365_box` | 6 990 | `365` + `office`/`microsoft` (без family/personal) |
| `default` | 9 990 | fallback |

**ВАЖНО:** проверка типа происходит в **указанном порядке** — `pro plus`
важнее чем `family`, потому что Pro Plus B2B-only. Если в title есть и
`pro plus` и `family` — берём `office_pro` (39 990).

### 🔍 titleOk (что считается контрафактом)
Карточка попадает в мониторинг **только если**:
- title содержит `office`, или
- title содержит и `365` и `microsoft`, или
- title содержит и `офис` и `microsoft`

**И не содержит**:
- `officesuite` (конкурент, не Microsoft)
- `р7-`, `р7 офис`, `мойофис`, `redos`, `ред ос`, `libreoffice`, `astra linux`,
  `базальт`, `rosa`, `кит офис` (отечественные альтернативы)
- `^код windows`, `^ключ windows`, `^windows \d`, `^лицензия windows`
  (только Windows, без Office)
- `книга`, `руководство`, `учебник`, `самоучитель`, `шаг за шагом`
  (книги о Office)

### 📊 Сортировка по умолчанию
Питон-предсортировка в `merge_mon_raw.py`:
```python
key=lambda r: (-disc_pct(r), r['price'], r['title'].lower())
```
JS-пресет `default` использует ту же тройку. **При равных дисконте и цене —
title по алфавиту** (детерминированный tiebreaker). Без вторичного ключа
карточки с одинаковым `disc=99%` шли в случайном порядке.

### 🧮 Computed flags (F1-F6)
```js
disc = round((1 - price / op) * 100)
F1 = disc >= 50
F2 = disc >= 80
F3 = (regDays !== null) ? (regDays < 30) : null   // продавец моложе 30 дней
F4 = (auth === false)                              // не авторизованный реселлер
F5 = true   // ВСЕГДА true с 03.12.2025 (телефонная активация отключена)
F6 = false  // TODO: ТЗ без пометки «совместимо»
```

### 📤 Экспорт CSV — RFC 4180 (НЕ забыть экранирование!)
- BOM `﻿` для UTF-8 в Excel
- CRLF (`\r\n`) переводы строк
- Поля с `,` `"` `\n` `\r` оборачиваются в кавычки, **внутренние `"` удваиваются**
- 225 карточек в текущем срезе содержат `,` в title — без этого экранирования
  CSV ломается (после первой запятой данные «уезжают» в соседние колонки)

### 🌐 Канал парсинга
- **CloakBrowser + VDSina SOCKS5** — единственный канал, который работает
  для всех 4 площадок. Подделывает JA3/HTTP-2/TLS-fingerprint так, что
  antibot маркетплейсов (Datadome на Avito, Imperva на Ozon, Akamai на ЯМ)
  не отличает запрос от настоящего «российского» браузера
- VDSina-туннель: `ssh -D 1080 -fN -i ~/.ssh/VDSina root@94.103.89.251`
- Подробности других каналов и почему они НЕ работают для всех — в § 3

### 🔎 6 поисковых запросов на мониторинге (= MON_QUERIES_DEFAULT)
1. Microsoft Office ключ активации
2. Microsoft Office 365 ключ
3. Microsoft Office 2021 ключ
4. Office 2021 ключ активации
5. Office 2024 ключ активации
6. MS Office ключ активации

Хранятся в localStorage, версионируются через `MON_QUERIES_VERSION=2`
(при смене дефолта старая версия сбрасывается).

---

## 1. Что это и зачем

Проактивный мониторинг карточек **контрафактных ключей Microsoft Office**
на российских маркетплейсах. Дашборд собирает:

- **Где** продаются нелицензионные ключи (Ozon, Avito, Wildberries, Я.Маркет)
- **По каким ценам** и какой это даёт дисконт от официальной цены Microsoft
- **Сколько** карточек содержит «красные флажки» F1-F6 (п. 2.2 Модельных
  практик ФАС/МСАП СНГ 2025)

Источник мониторинга — ассоциации [АПКИТ](https://apkit.ru/),
[АРПП «Отечественный софт»](https://arppsoft.ru/),
[НП «РУССОФТ»](https://russoft.org/),
[НП ППП](https://npppp.ru/). Чат-канал с маркетплейсами организует АПКИТ.

---

## 2. Архитектура проекта

```
anti-ms-mp/
├── index.html                 — каталог 3 дашбордов в корне (3 кнопки)
├── anti-ms-dashboard/
│   ├── index.html             — основной дашборд (текущая логика)
│   ├── index_new.html         — НОВАЯ версия на Supabase (соседняя сессия)
│   └── assets/
│       ├── favicon-32.png
│       ├── favicon-180.png    — apple-touch-icon
│       └── favicon-192.png    — android home screen
├── scraper/
│   ├── scrape-marketplaces.js — старый Playwright-парсер (для WB/ЯМ через VDSina)
│   ├── import_cloak_full.py   — импорт всех 4 источников + titleOk + official_price
│   ├── import_ozon_chrome.py  — резервный импортёр Ozon через Chrome JSON API
│   ├── import_avito_cloak.py  — резервный импортёр Avito
│   ├── merge_mon_raw.py       — мерж в index.html, обновление штампа scanLastTime
│   ├── avito_snippet.js       — DevTools-сниппет для ручной выгрузки Avito
│   ├── scrape-with-captcha.js — headful Playwright (резерв с ручной капчей)
│   ├── mon_data.json          — свежие WB+ЯМ (по умолчанию для merge)
│   ├── mon_data_captcha.json  — свежие Ozon+Avito (CloakBrowser+VDSina)
│   ├── SCRAPING_ALGORITHMS.md
│   └── SKILL.md
├── cloakbrowser-lab/          — ❌ В .gitignore — локальные эксперименты:
│   ├── scrape_ozon_cloak.py   — CloakBrowser-парсер Ozon
│   ├── scrape_avito_cloak.py  — CloakBrowser-парсер Avito
│   ├── scrape_wb_cloak.py     — CloakBrowser-парсер WB
│   ├── scrape_ym_cloak.py     — CloakBrowser-парсер ЯМ
│   ├── *_cloak_full.json      — сырые результаты парсинга (вход для import_cloak_full.py)
│   └── .venv/                 — Python venv с cloakbrowser==0.3.30
└── docs/                      — архитектурные заметки
```

**Пайплайн обновления данных** (вручную, периодически):

```bash
# 1. Поднять SOCKS5-туннель через российский VPS
ssh -D 1080 -fN -i ~/.ssh/VDSina root@94.103.89.251

# 2. Запустить CloakBrowser-парсинг (4 площадки параллельно)
cd cloakbrowser-lab
SCRAPER_PROXY=socks5://127.0.0.1:1080 .venv/bin/python scrape_avito_cloak.py \
  --queries "Microsoft Office ключ активации" "Microsoft Office 365 ключ" \
            "Microsoft Office 2021 ключ" "Office 2024 ключ активации" \
            "MS Office ключ активации" \
  --pages 15 --output avito_cloak_full.json
# (то же для ozon/wb/ym)

# 3. Импорт + мерж в HTML
cd ../scraper
python3 import_cloak_full.py        # читает cloakbrowser-lab/*_cloak_full.json,
                                     # применяет titleOk и official_price,
                                     # пишет mon_data.json + mon_data_captcha.json
python3 merge_mon_raw.py             # вставляет MON_RAW в anti-ms-dashboard/index.html,
                                     # обновляет штамп «Последнее сканирование»

# 4. Коммит + push на оба ремоута
git add anti-ms-dashboard/index.html scraper/mon_data*.json
git commit -m "scraper: обновление ..." && git push gitverse main && git push origin main
```

**Публикация**:
- **Vercel** (через GitHub): https://anti-ms-mp.vercel.app/anti-ms-dashboard/
- **Gitverse Pages**: https://isolovyev.gitverse.site/anti-ms-mp/anti-ms-dashboard/

---

## 3. Парсинг — каналы и их особенности

Российские маркетплейсы блокируют скрейпинг с зарубежных IP. Сегодня
известно **3 рабочих канала**:

| Канал | WB | ЯМ | Ozon | Avito | Когда применять |
|---|:-:|:-:|:-:|:-:|---|
| Playwright + VDSina SOCKS5 | ✅ | ✅ | ❌ | ❌ | Массовый bulk WB/ЯМ |
| Chrome → AmneziaVPN → JSON API | ✅ | ✅ | ✅ | ❌ | Ozon (composer-api.bx) |
| **CloakBrowser + VDSina SOCKS5** | ✅ | ✅ | ✅ | ✅ | **Универсальный** |

`CloakBrowser` подделывает JA3 / HTTP/2 / TLS fingerprint так, что antibot
маркетплейсов не отличает его от настоящего «российского» браузера. Это
**основной** канал сегодня.

### Известные баги парсинга (исправлены, но могут вернуться)

1. **NBSP в ценах**: WB и Я.Маркет разделяют тысячи неразрывным пробелом
   U+00A0. Regex `[\d ]{2,7}` ловит только U+20 — из `2 021 ₽` выходит `21`.
   Фикс: `replace(/[\s   ]+/g, ' ')` перед regex.

2. **Кешбэк на Ozon**: рядом с ценой Ozon показывает «Кешбэк 49 ₽». Старая
   логика брала `min(prices)` — попадал кешбэк. Фикс: `min(prices >= 100)`.

3. **Lazy-load script-карточки на Я.Маркет**: `article[data-auto="searchOrganic"]`
   до прокрутки содержит JS-код apiary вместо контента. Фильтр:
   skip если `innerText` начинается с `(window.|apiary*|^{"widgets"`.

4. **Avito ловит CDP-расширения**: расширение Claude in Chrome ловится
   Distil/Imperva — нужен CloakBrowser (имитирует чистый браузер).

5. **YM dynamic pricing (НЕ баг)**: в search-выдаче цена 32 ₽, на странице
   товара 28 ₽ — Яндекс показывает разные цены в разных view. Парсер
   берёт цену из search-выдачи — это то, что видит покупатель первым.

---

## 4. Структура данных MON_RAW

Главный массив на странице — `const MON_RAW = [...]` (примерно строка 3974
в `index.html`). Каждый элемент:

```js
{
  date: '2026-05-27',                       // ISO дата парсинга
  pl: 'avito',                              // 'avito' | 'ozon' | 'wildberries' | 'yandex'
  id: 'AV-1928294174',                      // префикс {OZ|AV|WB|YM}- + native cardId
  query: 'Microsoft Office ключ активации',  // какой поисковый запрос дал эту карточку
  title: 'Office 2019 professional plus...', // оригинальный заголовок, обрезан до 120 симв
  url: 'https://www.avito.ru/.../1928294174', // прямая ссылка на карточку
  price: 140,                                // цена в рублях (целое)
  op: 39990,                                 // официальная цена Microsoft (см. § 5.2)
  regDays: null,                             // возраст продавца в днях (TODO, пока не парсится)
  auth: null                                 // авторизованный реселлер Microsoft (TODO)
}
```

**При рендере** в `MON_DATA` каждой карточке добавляется поле
`flags = {disc, F1, F2, F3, F4, F5, F6}` через `monComputeFlags()`.

---

## 5. Алгоритмы

### 5.1 `titleOk(title)` — фильтр контрафакта

Карточка попадает в мониторинг только если её title описывает **Microsoft Office**.
Логика в `scraper/import_cloak_full.py` (Python) и `scraper/scrape-marketplaces.js` (JS).

**Включается**:
- содержит подстроку `office`
- или (`365` + `microsoft`)
- или (`офис` + `microsoft`)

**Исключается**:
- `officesuite` (конкурент, не Microsoft)
- `р7-`, `р7 офис`, `мойофис`, `redos`, `ред ос`, `libreoffice`, `astra linux` (российский софт)
- `^код windows`, `^ключ windows`, `^windows \d`, `^лицензия windows` (Windows-only, не Office)
- содержит `книга`, `руководство`, `учебник`, `самоучитель`, `шаг за шагом` (книги о Office, не лицензии)
- `р7-офис`, `мойофис`, `базальт`, `rosa`, `кит офис` — отечественные альтернативы

### 5.2 `official_price(title)` — определение «честной» цены Microsoft

Уточнённая логика (8 типов) в `scraper/import_cloak_full.py`:

| Ключ | op (₽) | Признаки в title |
|---|---:|---|
| `office_pro` (B2B-only) | 39 990 | `pro plus`, `professional plus`, `pro+`, `ltsc` |
| `office_home_bus` | 22 990 | `home and business`, `для работы`, ` h&b` |
| `office_home_student` | 14 990 | `home and student`, `для дома и учёбы` / `для дома и учебы` |
| `m365_family` | 5 290 | `family`, `семь`, `для семьи`, `family pack` |
| `m365_personal` | 2 790 | `personal`, `персональн`, `m365`, `1тб onedrive` |
| `office365_box` | 6 990 | `365` + `office`/`microsoft` (без family/personal маркера) |
| `office_home_student` (fallback) | 14 990 | `2024`, `2021`, `2019`, `2016` без редакции |
| `default` | 9 990 | всё остальное (редкий случай) |

**Фильтр после расчёта op**: карточки с `price >= 0.5 × op` отбрасываются
как **не контрафакт** (легальные товары со скидкой <50%). Это убирает
карточки с положительным «дисконтом» 30-40% и карточки с **завышенной**
ценой (отрицательный «дисконт»).

### 5.3 `computeFlags(r)` — красные флажки F1-F6

```js
function monComputeFlags(r) {
    const disc = Math.round((1 - r.price / r.op) * 100);
    return {
        disc,                                // вычисленный дисконт в %
        F1: disc >= 50,                      // скидка ≥50% от op
        F2: disc >= 80,                      // скидка ≥80% от op
        F3: r.regDays !== null ? (r.regDays < 30) : null,  // продавец моложе 30 дней
        F4: r.auth === false,                // не авторизованный реселлер MS
        F5: true,                            // F5 всегда true с 03.12.2025 (телефонная активация отключена)
        F6: false                            // ТЗ без «совместимо» — пока не парсится
    };
}
```

В UI отображение:
- 🔴 **F2** (красный) — дисконт ≥80%
- 🔴 **F1** (красный) — дисконт ≥50% (показывается если F2=false)
- 🟡 **F3** — продавец моложе 30 дней (или **F3?** в серой пилюле — нет данных)
- 🟡 **F4** — не авторизованный реселлер (или F4?)
- 🟡 **F5** — телефонная активация Microsoft отключена с 03.12.2025
- 🟠 **F6** — ТЗ без пометки «совместимо»

---

## 6. Сортировка

### 6.1 Предсортировка в Python (`merge_mon_raw.py`)

При записи `MON_RAW` массив сортируется по ключу:

```python
(-disc_pct(r), r['price'], r['title'].lower())
```

Т.е. **дисконт ↓ → цена ↑ → название ↑**. Это совпадает с пресетом
`default` в UI — пользователь видит ровно тот же порядок, что записан.

### 6.2 Пресеты сортировки в UI

Шесть кнопок над таблицей (`#monSortPresetBtns`):

| Пресет | Логика | Когда применять |
|---|---|---|
| 📊 **По умолчанию** | disc↓ → price↑ → title↑ | Главный сценарий: сначала 100% скидки, внутри — самые дешёвые |
| 🔥 **Самые подозрительные** | `score(r)` ↓ | Composite-индекс: дисконт + бонус за флажки + бонус за низкую цену |
| 💸 **Крупнейшие потери** | `(op−price)` ↓ | Сверху — где Microsoft теряет максимум денег за 1 покупку |
| 👥 **Группы товаров** | размер «семьи» ↓ | Кластер по нормализованному названию (`Office 2021 Pro Plus`) — массовый контрафакт |
| 🏪 **По площадкам** | pl A→Z → disc↓ → price↑ | Сначала весь Avito, затем Ozon, и т.д. — удобно при адресных обращениях |
| 🆕 **Свежие** | date↓ → disc↓ → price↑ | Когда появится timeseries из Supabase |

### 6.3 `monScore(r)` — индекс подозрительности

```js
function monScore(r) {
    let s = Math.max(0, r.flags.disc);            // 0..100
    s += monFlagCount(r.flags) * 3;               // до +18 (6 флажков)
    if(r.price < 100) s += 10;
    else if(r.price < 300) s += 5;
    if(r.op > 10000 && r.price < r.op * 0.01) s += 5;  // приманка
    return s;
}
```

### 6.4 `monFamilyKey(title)` — нормализация для кластеров

```js
function monFamilyKey(title) {
    let s = String(title||'').toLowerCase();
    const m = s.match(/(office\s+(?:365|2024|2021|2019|2016|2013|2010))\s*(pro\s*plus|home\s*and\s*business|home\s*and\s*student|professional\s*plus|professional|personal|family|standard|ltsc)?/i);
    if(m) return (m[1] + ' ' + (m[2]||'')).replace(/\s+/g,' ').trim();
    if(s.includes('365') && s.includes('microsoft')) return 'microsoft 365';
    return 'прочее';
}
```

Примеры кластеров: `office 2021 pro plus`, `office 2024 ltsc`, `office 365`,
`microsoft 365`. Размер кластера = `famCount.get(key)`.

### 6.5 Сортировка по клику на колонку

Клик по `<th>` (`monSortBy(col)`):
- Первый клик → ▲ (asc), повторный → ▼ (desc)
- Сбрасывает активный пресет (`sortPreset='col'`), индикатор пресета гаснет
- Тайбрейкеры:
  - Если первичная колонка = `disc` → price↑ → title↑
  - Иначе → disc↓ → title↑ (стабильность)

---

## 7. Фильтры

Все фильтры в `monState`:

```js
{
    pl: 'all',           // 'all' | 'ozon' | 'avito' | 'wildberries' | 'yandex' | 'aliexpress'
    disc: 0,             // 0 | 50 | 80 | 95 — минимальный дисконт в %
    flag: 0,             // 0 | 2 | 3 — минимальное число активных флажков
    search: '',          // поиск по названию (case-insensitive substring)
    query: '',           // фильтр по поисковому запросу (см. MON_QUERIES)
    page: 0,
    pageSize: 50,        // 10 | 50 | 100
    sortCol: '',
    sortDir: 1,
    sortPreset: 'default'
}
```

`MON_QUERIES` (поисковые запросы на мониторинге) — расширяемый список,
сохраняется в `localStorage`. Версионируется через `MON_QUERIES_VERSION=2`
(при апгрейде дефолта старый localStorage сбрасывается).

`MON_QUERIES_DEFAULT`:
1. Microsoft Office ключ активации
2. Microsoft Office 365 ключ
3. Microsoft Office 2021 ключ
4. Office 2021 ключ активации
5. Office 2024 ключ активации
6. MS Office ключ активации

---

## 8. Экспорт CSV / XLSX

Обе кнопки в шапке раздела «Мониторинг» (`exportMonCSV`, `exportMonXLSX`).

**Колонки**:

| # | Поле | Источник |
|---|---|---|
| 1 | Площадка | `MON_PL_LABEL[r.pl]` |
| 2 | ID | `r.id` (с префиксом OZ-/AV-/WB-/YM-) |
| 3 | Название | `r.title` |
| 4 | URL | `r.url` |
| 5 | Цена (руб.) | `r.price` |
| 6 | Офиц. цена (руб.) | `r.op` |
| 7 | Дисконт (%) | `r.flags.disc` |
| 8 | Флажки | конкатенация `F1;F2;F5...` |
| 9 | Дата обнаружения | `r.date` |
| 10 | Дата выгрузки | сегодня |

**CSV** — RFC 4180:
- BOM `﻿` для UTF-8 в Excel
- CRLF (`\r\n`) переводы
- Поля с `,` `"` `\n` `\r` обёрнуты в кавычки, внутренние `"` удвоены

**XLSX** через SheetJS (`xlsx.full.min.js@0.18.5` с CDN):
- Лист «Мониторинг»
- Ширины колонок: `[12,18,50,45,10,12,10,20,12,12]`

Экспортируется **отфильтрованный** набор `monFiltered` (не весь MON_RAW).

---

## 9. Площадки и URL карточек

| pl | Метка в UI | Стиль (фон/текст) | Префикс ID | Формат URL карточки |
|---|---|---|---|---|
| `ozon` | OZON | `#e3f2fd` / `#1565c0` | `OZ-` | `https://www.ozon.ru/product/{slug}-{cardId}/` |
| `wildberries` | Wildberries | `#f3e5f5` / `#6a1b9a` | `WB-` | `https://www.wildberries.ru/catalog/{cardId}/detail.aspx` |
| `yandex` | Яндекс.Маркет | `#fff3e0` / `#e65100` | `YM-` | `https://market.yandex.ru/card/{slug}/{cardId}` |
| `avito` | Авито | `#e8f5e9` / `#2e7d32` | `AV-` | `https://www.avito.ru/{city}/igry_pristavki_i_programmy/{slug}_{cardId}` |
| `aliexpress` | AliExpress | `#fff8e1` / `#e65100` | `AL-` | `https://aliexpress.ru/item/{cardId}.html` (не используется с 03.2026) |

---

## 10. Текущее состояние (для контекста соседней сессии)

На **2026-05-27** последний прогон CloakBrowser+VDSina дал:

| Площадка | Карточек после titleOk + (price < 0.5×op) |
|---|---:|
| Avito | 665 |
| Wildberries | 289 |
| Я.Маркет | 138 |
| Ozon | 88 |
| **Итого** | **1 180** |

Распределение дисконтов:
- 50-79% — 58
- 80-94% — 224
- 95-98% — 426
- 99%+ — 472 (явные приманки)

---

## 11. Что нужно для `index_new.html` (Supabase)

Соседняя сессия делает версию, которая тянет данные не из захардкоженного
`MON_RAW`, а из Supabase. Схема таблицы (она же `scraper/upload_raw_to_supabase.py`):

```sql
CREATE TABLE listings (
    id           SERIAL PRIMARY KEY,
    date         DATE NOT NULL,
    pl           TEXT NOT NULL,          -- 'ozon', 'ym', 'wb', 'avito'
    product_id   TEXT NOT NULL UNIQUE,   -- 'OZ-{cardId}', 'AV-{cardId}', ...
    query        TEXT,
    title        TEXT NOT NULL,
    url          TEXT,
    price        INTEGER,
    op           INTEGER,
    reg_days     INTEGER,                -- для F3
    auth         BOOLEAN,                -- для F4
    scraped_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Индексы для дашборда
CREATE INDEX idx_listings_pl ON listings(pl);
CREATE INDEX idx_listings_scraped_at ON listings(scraped_at DESC);
CREATE INDEX idx_listings_op_price ON listings(op, price);  -- для расчёта disc
```

Supabase project: `yqfdbuiyfkzhkhpiknob` (eu-west-1).

### Что должно быть в `index_new.html`:

1. **Таб «Мониторинг» — первый и активный** при загрузке (как в текущей версии)
2. **Те же фильтры**: площадка, дисконт, флажки, поиск, query-теги
3. **Те же 6 пресетов сортировки** (см. § 6.2)
4. **Те же индикаторы**: ▲/▼ для активной колонки, ↕ для остальных
5. **Экспорт CSV (RFC 4180) + XLSX** — без поломки на запятых
6. **Расчёт `op` на стороне клиента** — на случай если в Supabase лежит сырой
   `op` без учёта новых типов; или хранить актуальный `op` в БД (см. § 5.2)
7. **`monComputeFlags()`** на клиенте — таким же
8. **MON_QUERIES в localStorage** с версионированием
9. **Штамп «Последнее сканирование»** — `MAX(scraped_at)` из БД
10. **«Всего записей»** — `COUNT(*)` после применённых фильтров, не клиентский
    `MON_RAW.length` (когда карточек будут тысячи — выкачивать всё нельзя)

### Точки пагинации/выборки в Supabase:

```js
// Простой случай — ≤2000 карточек, грузим всё за раз
const { data } = await supabase
    .from('listings')
    .select('*')
    .order('scraped_at', { ascending: false })  // только последний срез
    .limit(2000);

// Когда вырастет — серверная пагинация + сортировка/фильтры в URL
```

Расчёт дисконта **на клиенте** (тот же `monComputeFlags`) — чтобы не
требовать SQL-вью каждый раз когда `OFFICIAL_PRICES` меняется.

---

## 12. История изменений (хронология за 26-27 мая 2026)

### День 1 (26.05): возрождение парсинга

| Коммит | Что |
|---|---|
| `41b398c` | Playwright + VDSina SOCKS5 → WB 278 + ЯМ 164 (Ozon/Avito недоступны через datacenter IP) |
| `f9ab27d` | Chrome → AmneziaVPN → Ozon JSON API (composer-api.bx) → 101 карточка |
| `cd31eee` | CloakBrowser Avito (первый успех) → +30 свежих к 737 старым |
| `1cac745` | Полная замена снапшота: CloakBrowser+VDSina все 4 площадки → 1291 карточка |

### День 2 (27.05): фиксы качества данных

| Коммит | Что |
|---|---|
| `201a7df` | Штамп «Последнее сканирование» автоматически в `merge_mon_raw.py` |
| `19a7b6e` | Таб «Мониторинг» первым, активным по умолчанию; favicon из аватарки чата АПКИТ |
| `8e2595d` | Гиперссылки на сайты всех 4 ассоциаций |
| `b050c66` | **Фикс NBSP** в WB DOM-парсере (`2 021 ₽` → `2021` вместо `21`) |
| `da17f84` | **Уточнённые op (8 типов)** + фильтр `price >= 0.5×op` (не контрафакт) |
| `0dea094` | Предсортировка в Python: `(-disc, price, title)` |
| `34d2308` | UI-индикаторы сортировки колонок (▼/▲ для активной, ↕ для остальных) |
| `7b71492` | **CSV-экспорт RFC 4180** (225 карточек с `,` в title больше не ломают выгрузку) |
| `a988fea` | Мультиключевая JS-сортировка (disc↓ → price↑ → title↑ детерминированно) |
| `986b530` | **6 пресетов сортировки** + первая версия `MONITORING_LOGIC.md` |
| `b0a113f` | Пресет подсвечивает соответствующую колонку стрелкой ▼/▲ |

### Итог за 2 дня

| Площадка | Утром 26.05 (снапшот 19.03) | Конец 27.05 (b0a113f) |
|---|---:|---:|
| Wildberries | 61 | **289** |
| Avito | 737 | **665** |
| Я.Маркет | 1 186¹ | **138** |
| Ozon | 74 | **88** |
| AliExpress | 3 | 0 |
| **ИТОГО** | **2 061** | **1 180** |

¹ Старая цифра ЯМ — раздутые повторы без дедупа. Реальных уникальных было ~150.

Полная история: `git log --oneline -30`.

---

## 13. Чеклист для index_new.html (Supabase)

Перед началом портирования логики в новую страницу:

- [ ] Подключиться к Supabase project `yqfdbuiyfkzhkhpiknob` (eu-west-1), таблица `listings`
- [ ] Получить `MAX(scraped_at)` для штампа «Последнее сканирование»
- [ ] Получить выборку с `LIMIT 2000` (или server-side пагинация)
- [ ] **Применить titleOk на клиенте** (или хранить отфильтрованным в БД)
- [ ] **Применить фильтр `price >= 0.5 × op`** (не контрафакт)
- [ ] **Считать op на клиенте** через ту же логику 8 типов (см. § 5.2) — на случай
      если в Supabase лежит сырой op без учёта обновлённой методики
- [ ] **monComputeFlags** портировать как есть
- [ ] **6 пресетов сортировки + индикаторы ▼/▲** портировать целиком
- [ ] **CSV-экспорт с RFC 4180 экранированием** портировать целиком
- [ ] **MON_QUERIES в localStorage** с версионированием
- [ ] **Дедупликация по `(pl, id)`** — если БД может содержать дубли при
      многократном парсинге одного товара, добавить `DISTINCT ON (pl, id)`
      с самой свежей `scraped_at`
- [ ] Таб «Мониторинг» — первый и активный
- [ ] Favicon из `assets/`
- [ ] Гиперссылки на все 4 ассоциации
- [ ] Кнопки CSV/XLSX вверху, после фильтров

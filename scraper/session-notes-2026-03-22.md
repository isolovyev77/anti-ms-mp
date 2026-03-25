# Сессия: Парсинг Ozon → Supabase
**Дата:** 2026-03-22
**Проект:** anti-ms-mp (мониторинг нелицензионного ПО Microsoft на маркетплейсах)

---

## Что сделано

- Собраны карточки Office с Ozon через **Claude in Chrome** (реальный браузер, российский IP 176.62.187.214, Истра) - Ozon не блокирует
- Применены фильтры `title_ok` (содержит 'office' ИЛИ '365'+'microsoft') и цена 50-5000 ₽
- Исключены продукты OfficeSuite (конкурент, не Microsoft)
- Вставлено **72 карточки** в таблицу `listings` Supabase (project: `yqfdbuiyfkzhkhpiknob`)
  - 11 - старые (из предыдущих сессий)
  - 61 - новые из `ozon_raw.txt` (355 строк, дата файла 19 марта)

### Статистика listings (ozon):
- Всего: 72 карточки
- Цены: 115 - 3587 ₽
- Средняя цена: 541 ₽
- op (официальные цены): 6990 / 9990 / 13990 / 14990 / 16990 / 22990

---

## Ключевые выводы

### Почему Firecrawl не работает с Ozon
Firecrawl делает запросы с американских IP через proxy-серверы - Ozon их блокирует (возвращает 403). Это не решается ни stealth, ни enhanced-proxy режимами. Единственный надёжный способ - **реальный браузер с российским IP**.

### Почему Claude in Chrome - лучший инструмент для Ozon
- Это настоящий Chrome, работающий на машине пользователя с его IP
- Не обнаруживается как headless/bot
- JS-коллектор через `javascript_tool` собирает данные прямо из DOM
- Данные накапливаются между страницами через `sessionStorage`

### Почему Playwright headless не работает на Ozon
- Ozon детектирует headless-браузеры по `navigator.webdriver`, отсутствию плагинов, etc.
- Даже с `headless:false` нужен российский IP
- На VPS - проблема двойная: нет российского IP + нет настоящего дисплея

### Про VM (Cowork):
- VM имеет egress proxy, который блокирует прямые исходящие соединения (curl, python urllib, playwright - всё через 403 tunnel)
- В VM нельзя сделать ни один прямой HTTP запрос к внешнему сайту
- Playwright в VM бесполезен для Ozon по двум причинам: proxy-блок + bot-детект

---

## Архитектура данных

### Таблица listings (Supabase)
```sql
CREATE TABLE listings (
  id          SERIAL PRIMARY KEY,
  date        DATE NOT NULL,
  pl          TEXT NOT NULL,           -- 'ozon', 'ym', 'wb', 'avito', etc.
  product_id  TEXT NOT NULL UNIQUE,    -- 'OZ-{cardId}'
  query       TEXT,
  title       TEXT NOT NULL,
  url         TEXT,
  price       INTEGER,
  op          INTEGER,                 -- официальная цена продукта
  reg_days    INTEGER,                 -- возраст аккаунта (для Avito/YM)
  auth        BOOLEAN,                 -- авторизованный продавец?
  scraped_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Формат ozon_raw.txt
```
price|cardId|query|title
```
Пример: `157|3387333078|Microsoft Office ключ активации|Office 365 Pro Plus...`

### op-функция (определение официальной цены)
```python
def get_op(title):
    t = title.lower()
    if '365' in t or 'personal' in t or 'подписк' in t: return 6990
    if 'home and business' in t or 'home & business' in t or 'для дома и бизнеса' in t: return 22990
    if '2021' in t: return 14990
    if 'windows 11' in t and 'pro' in t: return 16990
    if 'windows 11' in t: return 13990
    if 'windows 10' in t: return 13990
    return 9990
```

### title_ok фильтр
```python
def title_ok(title):
    t = title.lower()
    if 'officesuite' in t: return False  # конкурент, не Microsoft
    return 'office' in t or ('365' in t and 'microsoft' in t)
```

---

## JS-коллектор для Ozon (Claude in Chrome)

```javascript
async function collectPage(query) {
  // Скролл для подгрузки lazy-loaded карточек
  for (let i = 1; i <= 10; i++) {
    window.scrollTo(0, document.body.scrollHeight * i / 10);
    await new Promise(r => setTimeout(r, 300));
  }
  await new Promise(r => setTimeout(r, 1000));

  const items = [];
  const seen = new Set();
  const links = document.querySelectorAll('a[href*="/product/"]');

  for (const a of links) {
    const m = a.href.match(/-(\d{5,12})(?:\/|\?|$)/);
    if (!m) continue;
    const cardId = m[1];
    if (seen.has(cardId)) continue;
    seen.add(cardId);

    // Подняться до .tile-root чтобы получить всю карточку с ценой
    const container = a.closest('.tile-root') || a.closest('[class*="tile"]');
    if (!container) continue;

    const text = container.innerText || '';
    const priceMatch = text.match(/(\d[\d\s]{1,5})\s*₽/);
    if (!priceMatch) continue;
    const price = parseInt(priceMatch[1].replace(/\s/g, ''));
    if (!price || price < 50 || price > 5000) continue;

    const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 15);
    const title = lines.find(l =>
      !l.includes('₽') && !l.includes('%') && !l.includes('отзыв') &&
      !l.includes('Распродажа') && !l.includes('осталось') &&
      !l.includes('Цифровой') && !l.includes('морковок')
    ) || '';
    if (title.length < 5) continue;

    const tl = title.toLowerCase();
    if (!tl.includes('office') && !(tl.includes('365') && tl.includes('microsoft'))) continue;

    items.push({ cardId, price, title: title.slice(0, 120), query });
  }

  // Дедупликация через sessionStorage (накапливаем между страницами)
  const prev = JSON.parse(sessionStorage.getItem('oz_collect') || '[]');
  const prevIds = new Set(prev.map(r => r.cardId));
  const newItems = items.filter(r => !prevIds.has(r.cardId));
  const all = [...prev, ...newItems];
  sessionStorage.setItem('oz_collect', JSON.stringify(all));
  return { newThisPage: newItems.length, total: all.length };
}
```

**Выгрузка результатов:**
```javascript
// Запустить в консоли после обхода всех страниц
const data = JSON.parse(sessionStorage.getItem('oz_collect') || '[]');
const blob = new Blob([data.map(r => `${r.price}|${r.cardId}|${r.query}|${r.title}`).join('\n')], {type:'text/plain'});
const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
a.download = 'ozon_new.txt'; a.click();
```

---

## Playwright / Chromium на VPS

Playwright скачивает свой собственный Chromium (~280 MB) отдельно от системного Chrome:
- Хранится в `~/.cache/ms-playwright/chromium-XXXX/`
- Скачивается один раз: `npx playwright install chromium`
- На VPS дополнительно нужны системные зависимости: `npx playwright install-deps chromium`
- После первой установки повторного скачивания нет

**На macOS (машина пользователя):** если `npx playwright install chromium` уже запускался - Chromium уже есть, перекачивать не нужно.

---

## Рекомендации для последующей работы

### 1. Регулярный сбор данных с Ozon
- Запускать JS-коллектор через Claude in Chrome раз в неделю (или по запросу)
- Сохранять результат blob-скачиванием → добавлять в ozon_raw.txt
- Перегонять в Supabase через compile_mon_raw.py или аналогичный Python-скрипт
- **Не использовать** Firecrawl, curl из VM, headless Playwright для Ozon

### 2. Другие маркетплейсы
- **Яндекс Маркет, Avito**: аналогичный JS-коллектор через Claude in Chrome (SKILL.md содержит рабочие примеры)
- **Wildberries**: прямой API работает, но WB редиректит на preset (нужен другой endpoint или тот же подход через Chrome)
- **AliExpress**: Claude in Chrome, свой JS-коллектор (есть в SKILL.md)

### 3. Автоматизация через VPS (если нужна)
- Headless Playwright обнаруживается Ozon - бесполезен
- Альтернатива на VPS: настроить VPN с российским IP + `headless:false` + `xvfb` (виртуальный дисплей)
- Либо использовать residential proxy с российскими IP (дорого)
- **Проще**: запускать сбор вручную через Claude in Chrome на своей машине

### 4. Качество данных
- `reg_days` и `auth` пока NULL для всех Ozon-карточек - для Ozon эти данные сложнее получить (нет явного отображения возраста магазина на карточке товара)
- Стоит периодически обновлять цены (текущие данные от 19 марта)
- Карточки-комплекты ("Windows + Office") попадают в выборку - решить, нужны ли они

### 5. Структура проекта
- Основной дашборд: `/anti-ms-dashboard/index.html` (не ozon_dashboard - он устарел)
- Данные из Supabase можно подгружать в дашборд через fetch API (сейчас там статичный MON_RAW массив)
- Supabase project: `yqfdbuiyfkzhkhpiknob`, region: eu-west-1

---

*Заметка создана автоматически по итогам сессии 2026-03-22*

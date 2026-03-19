# Скилл: парсинг карточек на маркетплейсах

Все секретные приёмы, открытые в ходе проекта anti-ms-mp.
Используй этот файл как шпаргалку - не изобретай колесо заново.

---

## Общий воркфлоу

```
1. Открыть страницу результатов поиска в браузере
2. Запустить JS-коллектор в консоли (или через JavaScript tool)
3. Накапливать данные в sessionStorage между страницами
4. После всех страниц - экспортировать через blob download
5. Пользователь кладёт файл в папку scraper
6. Merge Python-скриптом в raw-файл
7. Запустить compile_mon_raw.py → обновит dashboard/index.html
```

---

## Формат raw-файлов

```
price|cardId|query|title
```

Пример:
```
217|2926983276|Microsoft Office ключ активации|Microsoft Office 2024 Pro Plus LTSC Ключ активации
```

Правила:
- UTF-8, одна запись на строку
- price - целое число в рублях
- cardId - уникальный ID карточки на площадке
- query - поисковый запрос которым найдена карточка
- title - до 120 символов

Файлы: `avito_raw.txt`, `ym_raw.txt`, `wb_raw.txt`, `ozon_raw.txt`

---

## ЯНДЕКС МАРКЕТ (ym_raw.txt)

### Поисковые запросы (2026)
```
Office 365 ключ
Office 2021 ключ
Office 2024 ключ
Office 2019 ключ
```

### URL пагинации
```
https://market.yandex.ru/search?text=ENCODED_QUERY&page=N
```
- Страница N начинается с 1
- Страница 20 = последняя страница (редиректит на главную, articles=1)
- ~17-20 карточек на страницу
- Признак конца: `articles: 1` и заголовок вкладки = "Яндекс Маркет" (без номера страницы)

### DOM-структура (2026)
```
article[id]                          - карточка товара
  ├─ id атрибут                      - cardId (alphanumeric, напр. "0x2h4qjmmya")
  ├─ h3 / [data-auto="snippet-title"] - заголовок
  └─ innerText (регекс)               - цена: /(\d[\d\s]{0,5})\s*₽/
```

### JS-коллектор (запускать на каждой странице)
```javascript
const query = 'Office 365 ключ'; // менять под текущий запрос
const items = [];
const seen = new Set();
const articles = document.querySelectorAll('article[id]');
for (const a of articles) {
  const cardId = a.getAttribute('id');
  if (!cardId || seen.has(cardId)) continue;
  seen.add(cardId);
  const titleEl = a.querySelector('h3, [data-auto="snippet-title"]');
  const title = titleEl ? titleEl.innerText.trim().replace(/\n/g,' ').slice(0,120) : '';
  if (!title || title.length < 5) continue;
  const priceMatch = a.innerText.match(/(\d[\d\s]{0,5})\s*₽/);
  const price = priceMatch ? parseInt(priceMatch[1].replace(/\s/g,'')) : 0;
  if (!price || price < 50 || price > 10000) continue;
  items.push({cardId, price, title, query});
}
const prev = JSON.parse(sessionStorage.getItem('ym4') || '[]');
const prevIds = new Set(prev.map(r => r.cardId));
const newItems = items.filter(r => !prevIds.has(r.cardId));
const all = [...prev, ...newItems];
sessionStorage.setItem('ym4', JSON.stringify(all));
({articles: articles.length, newThisPage: newItems.length, total: all.length});
```

### Экспорт через blob download
```javascript
const all = JSON.parse(sessionStorage.getItem('ym4') || '[]');
const lines = all.map(r => `${r.price}|${r.cardId}|${r.query}|${r.title}`);
const content = lines.join('\n');
const blob = new Blob([content], {type: 'text/plain;charset=utf-8'});
const url = URL.createObjectURL(blob);
const a = document.createElement('a');
a.href = url;
a.download = 'ym4_new.txt';
document.body.appendChild(a);
a.click();
document.body.removeChild(a);
// НЕ вызывать URL.revokeObjectURL сразу - иначе скачивание не успеет
```

**ВАЖНО**: Если браузер не показывает диалог скачивания, создай видимую кнопку-ссылку:
```javascript
const div = document.createElement('div');
div.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999999;background:#c62828;padding:16px;';
div.innerHTML = `<a href="${url}" download="ym4_new.txt" style="color:yellow;font-size:24px;">💾 НАЖМИ СЮДА — скачать</a>`;
document.body.appendChild(div);
```

### URL карточек YM
```
https://market.yandex.ru/product/{cardId}
```
cardId - alphanumeric строка (напр. `xep3ubtl9ng`, `0x2h4qjmmya`)

### Ключ sessionStorage
- `ym4` - для 4 новых запросов (Office 365/2021/2024/2019 ключ)
- Дедупликация по cardId накапливается автоматически между страницами

---

## АВИТО (avito_raw.txt)

### Поисковые запросы (2026)
```
Office 365 ключ
Office 2021 ключ
Office 2024 ключ
Office 2019 ключ
```

### URL пагинации
```
https://www.avito.ru/rossiya?q=ENCODED_QUERY&p=N
```
- Параметр `p=N`, начиная с 1
- Максимум 15 страниц (p=16 редиректит обратно на p=1)
- ~40 карточек на страницу
- Признак конца: страница N возвращает 0 новых items ИЛИ текст первой карточки совпадает с p=1

### DOM-структура (2026)
```
[data-item-id]                   - карточка товара
  ├─ data-item-id                - cardId (числовой, напр. 4264895760)
  ├─ h3 / [itemprop="name"]      - заголовок
  └─ [class*="price-root"] [class*="Price"] - цена (текст типа "150 ₽")
```

### JS-коллектор (запускать на каждой странице)
```javascript
const query = 'Office 365 ключ'; // менять под текущий запрос
const items = [];
const seen = new Set();
const cards = document.querySelectorAll('[data-item-id]');
for (const c of cards) {
  const cardId = c.getAttribute('data-item-id');
  if (!cardId || seen.has(cardId)) continue;
  seen.add(cardId);
  const titleEl = c.querySelector('h3, [itemprop="name"]');
  const title = titleEl ? titleEl.innerText.trim().replace(/\n/g,' ').slice(0,120) : '';
  if (!title || title.length < 5) continue;
  const priceEl = c.querySelector('[class*="price-root"] [class*="Price"]');
  const priceText = priceEl ? priceEl.innerText : '';
  const priceMatch = priceText.match(/(\d[\d\s]*)/);
  const price = priceMatch ? parseInt(priceMatch[1].replace(/\s/g,'')) : 0;
  if (!price || price < 50 || price > 10000) continue;
  items.push({cardId, price, title, query});
}
const prev = JSON.parse(sessionStorage.getItem('av4') || '[]');
const prevIds = new Set(prev.map(r => r.cardId));
const newItems = items.filter(r => !prevIds.has(r.cardId));
const all = [...prev, ...newItems];
sessionStorage.setItem('av4', JSON.stringify(all));
({cards: cards.length, newThisPage: newItems.length, total: all.length});
```

### Ключ sessionStorage
- `av4` - для 4 новых запросов
- Аналогично YM - дедупликация автоматическая

### URL карточек Авито
```
https://www.avito.ru/items/{cardId}
```
cardId - числовой (напр. 4264895760)

---

## WILDBERRIES (wb_raw.txt)

### URL пагинации
```
https://www.wildberries.ru/catalog/0/search.aspx?search=ENCODED_QUERY&page=N
```
или через API (предпочтительнее):
```
https://search.wb.ru/exactmatch/ru/common/v9/search?query=QUERY&page=N&sort=popular
```

### DOM-структура
```
article.product-card              - карточка
  ├─ a[data-nm-id]                - cardId в атрибуте data-nm-id
  ├─ span.product-card__name      - заголовок
  └─ ins.price__lower-price       - цена
```

### URL карточек WB
```
https://www.wildberries.ru/catalog/{cardId}/detail.aspx
```
cardId - числовой

---

## OZON (ozon_raw.txt)

### URL пагинации
```
https://www.ozon.ru/search/?text=ENCODED_QUERY&page=N&sorting=rating
```

### DOM-структура
```
div[data-widget="searchResultsV2"]
  └─ div[data-index] / div.tile-root    - карточка
       ├─ a[href*="/product/"]           - ссылка (cardId в конце URL)
       └─ span[class*="tsBody500Medium"] - цена
```

### Извлечение cardId из URL
```python
import re
match = re.search(r'-(\d{5,12})/$', product_url)
card_id = match.group(1) if match else None
```

### URL карточек OZON
```
https://www.ozon.ru/product/-{cardId}/
```
cardId - числовой (5-12 цифр в конце URL)

---

## Перенос данных из браузера в VM

**Проблема**: VM изолирован на 127.0.0.1, нет xclip/xsel. JS tool обрезает вывод ~1300 символов.

**Решение**: blob URL + видимая кнопка-ссылка → пользователь кликает → файл в Downloads → перемещает в папку scraper.

**Шаблон кнопки**:
```javascript
const blob = new Blob([content], {type: 'text/plain;charset=utf-8'});
const url = URL.createObjectURL(blob);
const div = document.createElement('div');
div.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999999;background:#c62828;padding:16px;';
div.innerHTML = `<a href="${url}" download="имя_файла.txt" style="color:yellow;font-size:24px;">💾 НАЖМИ СЮДА — скачать (${lines.length} строк)</a>`;
document.body.appendChild(div);
// НЕ удалять div и НЕ revoke URL - иначе ссылка пропадёт до клика
```

---

## compile_mon_raw.py

Скрипт `compile_mon_raw.py` (этот каталог) обновляет дашборд:
```
python3 compile_mon_raw.py
```

**Фильтры**:
- Цена 50-5000 руб. (дешевле - явно контрафакт, дороже - легитимный товар)
- Title содержит: office, microsoft, 365, 2021, 2024, 2019, 2016, ключ актив, лиценз
- Дедупликация по cardId

**Официальные цены (op) по типу продукта**:
| Продукт | op (руб.) |
|---------|-----------|
| Office 365 / Personal / подписка | 6990 |
| Office 2021 Home | 14990 |
| Office 2021 Home & Business | 22990 |
| Windows 11 Pro | 16990 |
| Windows 11 Home | 13990 |
| Windows 10 | 13990 |
| Прочее | 9990 |

---

## Ключевые уроки

1. **YM не имеет API пагинации** - только `?page=N` в URL. Страница 20 = конец (редирект на главную).

2. **Авито максимум 15 страниц** - page=16 редиректит на page=1. Признак: 0 новых items.

3. **sessionStorage переживает навигацию** в рамках одного домена. Это главный механизм накопления между страницами.

4. **YM "Ещё N" кнопка** загружает ещё товаров на той же странице (lazy load). Лучше использовать `?page=N` для чистых данных.

5. **Blob download может быть заблокирован** на странице-редиректе (e.g. Яндекс Маркет page=20). Решение: сначала перейти на нормальную страницу, потом делать download.

6. **Autoclick не всегда работает** в Chrome. Показывать видимую кнопку и ждать клика пользователя надёжнее.

7. **Дисконт округлять вниз** (floor, не round) - чтобы не показывать 100% скидку при цене 50 руб.

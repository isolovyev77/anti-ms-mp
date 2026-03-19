# MS Key Monitor - Playwright Scraper

Автоматический сбор листингов нелицензионных ключей Microsoft со всех
основных российских маркетплейсов (OZON, WB, Яндекс.Маркет, Авито).

## Установка

```bash
npm install
npx playwright install chromium
```

## Запуск

```bash
# Стандартный запуск - все площадки, все запросы, 5 страниц
npm run scrape

# Только OZON, 10 страниц
npm run scrape:ozon

# Свой запрос
node scrape-marketplaces.js --query "Windows 11 ключ активации" --pages 5

# Несколько площадок
node scrape-marketplaces.js --platforms ozon,wildberries --pages 3

# Видимый браузер (удобно при отладке / проблемах с капчей)
node scrape-marketplaces.js --headful
```

## Результаты

- `mon_data.json` - полный JSON с флажками F1-F6
- `mon_raw_snippet.js` - готовый JS-сниппет для вставки в `MON_RAW` дашборда

## Добавление данных в дашборд

1. Запустите скрепер: `npm run scrape:all`
2. Откройте `mon_raw_snippet.js`
3. Скопируйте массив из `const MON_RAW = [...]`
4. Вставьте в `index.html` на место существующего `MON_RAW`

## Настройка на VPS (cron каждые 6 часов)

```bash
# Установка
cd /opt
git clone <repo> ms-monitor
cd ms-monitor/scraper && npm install
npx playwright install chromium

# Добавить в crontab
crontab -e
```

```cron
# Мониторинг маркетплейсов каждые 6 часов
0 */6 * * * cd /opt/ms-monitor/scraper && node scrape-marketplaces.js >> /var/log/ms-monitor.log 2>&1
```

## Флажки F1-F6

| Флажок | Условие |
|--------|---------|
| F1 | Скидка >= 50% от официальной цены |
| F2 | Скидка >= 80% от официальной цены |
| F3 | Продавец зарегистрирован менее 30 дней назад |
| F4 | Продавец не является авторизованным партнёром Microsoft |
| F5 | Телефонная активация отключена (с 03.12.2025) |
| F6 | Товар маркирован TM без пометки "совместимо" |

F5 выставляется автоматически (всегда true после 03.12.2025).
F3, F4, F6 требуют дополнительных данных (регдата продавца, список партнёров).

## Требования к VPS

- Node.js >= 18
- RAM >= 1GB (Chromium)
- Ubuntu 20.04+ / Debian 11+
- Для скрытого запуска: `Xvfb` (если нужен headful в VPS без монитора)

```bash
# Установка виртуального дисплея для headful режима на VPS
apt install -y xvfb
Xvfb :99 -screen 0 1366x768x24 &
DISPLAY=:99 node scrape-marketplaces.js
```

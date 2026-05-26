#!/usr/bin/env node
/**
 * Scraper нелицензионных ключей Microsoft на российских маркетплейсах
 * Использует Playwright (Chromium) с имитацией человеческого поведения.
 *
 * Установка:
 *   npm install playwright
 *   npx playwright install chromium
 *
 * Запуск:
 *   node scrape-marketplaces.js
 *   node scrape-marketplaces.js --query "Windows 11 ключ активации" --pages 5
 *   node scrape-marketplaces.js --platforms ozon,wildberries --output data.json
 *
 * VPS cron (каждые 6 часов):
 *   0 *\/6 * * * cd /opt/ms-monitor && node scrape-marketplaces.js >> logs/cron.log 2>&1
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

// ========== КОНФИГУРАЦИЯ ==========
const CONFIG = {
  // Поисковые запросы для мониторинга (только Microsoft Office, без Windows)
  queries: [
    'Microsoft Office ключ активации',
    'Microsoft Office 365 ключ',
    'Office 2021 ключ активации',
    'Office 2024 ключ активации',
    'MS Office ключ активации',
  ],
  // Площадки: ozon | wildberries | yandex | avito
  platforms: ['ozon', 'wildberries', 'yandex', 'avito'],
  // Максимум страниц на запрос на площадку (1 стр ~ 36-48 товаров)
  maxPages: 5,
  // Задержки (мс) для имитации человека
  delay: { min: 1200, max: 3500 },
  // Headless режим (false - видимый браузер, удобно для отладки)
  headless: true,
  // Куда сохранять результаты
  outputFile: path.join(__dirname, 'mon_data.json'),
  // Официальные цены Microsoft (руб.) для расчёта дисконта
  officialPrices: {
    office365: 6990,
    office2021home: 14990,
    office2021homebus: 22990,
    windows11home: 13990,
    windows11pro: 16990,
    windows10: 13990,
    default: 9990,
  },
};

// ========== ПАРСИНГ АРГУМЕНТОВ ==========
// SOCKS5/HTTP прокси (для обхода блокировок маркетплейсов с иностранных IP).
// По умолчанию подхватываем из env (SCRAPER_PROXY / HTTPS_PROXY) — туннель
// поднимается командой: ssh -D 1080 -fN -i ~/.ssh/VDSina root@94.103.89.251
CONFIG.proxy = process.env.SCRAPER_PROXY || process.env.HTTPS_PROXY || null;

const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--query' && args[i+1]) { CONFIG.queries = [args[i+1]]; i++; }
  if (args[i] === '--pages' && args[i+1]) { CONFIG.maxPages = parseInt(args[i+1]); i++; }
  if (args[i] === '--platforms' && args[i+1]) { CONFIG.platforms = args[i+1].split(','); i++; }
  if (args[i] === '--output' && args[i+1]) { CONFIG.outputFile = args[i+1]; i++; }
  if (args[i] === '--proxy' && args[i+1]) { CONFIG.proxy = args[i+1]; i++; }
  if (args[i] === '--headful') { CONFIG.headless = false; }
}

// ========== УТИЛИТЫ ==========
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
const delay = () => sleep(rand(CONFIG.delay.min, CONFIG.delay.max));

function today() {
  return new Date().toISOString().slice(0, 10);
}

function detectOfficialPrice(title) {
  const t = title.toLowerCase();
  if (t.includes('365') || t.includes('personal') || t.includes('подписк')) return CONFIG.officialPrices.office365;
  if (t.includes('home and business') || t.includes('home & business') || t.includes('hb')) return CONFIG.officialPrices.office2021homebus;
  if (t.includes('2021') && t.includes('home')) return CONFIG.officialPrices.office2021home;
  if (t.includes('2021')) return CONFIG.officialPrices.office2021home;
  if (t.includes('windows 11') && t.includes('pro')) return CONFIG.officialPrices.windows11pro;
  if (t.includes('windows 11')) return CONFIG.officialPrices.windows11home;
  if (t.includes('windows 10')) return CONFIG.officialPrices.windows10;
  return CONFIG.officialPrices.default;
}

function calcDisc(price, op) {
  if (!op || op <= 0) return 0;
  return Math.round((1 - price / op) * 100);
}

function computeFlags(r) {
  const disc = calcDisc(r.price, r.op);
  return {
    disc,
    F1: disc >= 50,
    F2: disc >= 80,
    F3: r.regDays !== null ? (r.regDays < 30) : null,  // null = неизвестно
    F4: r.auth === false,
    F5: true,  // Телефонная активация отключена с 03.12.2025 - F5 всегда true
    F6: false, // TM без "совместимо" - требует ручной проверки
  };
}

/**
 * Фильтр заголовков — только Microsoft Office и все его вариации.
 * Исключает: Windows, Visio, Project, Adobe, МойОфис, OfficeSuite и прочее.
 */
function titleOk(title) {
  const t = title.toLowerCase();
  if (t.includes('officesuite')) return false;  // конкурент, не Microsoft
  return (
    t.includes('office') ||
    (t.includes('365') && t.includes('microsoft')) ||
    (t.includes('офис') && t.includes('microsoft'))
  );
}

function makeId(pl, idx, query) {
  const prefixes = { ozon: 'OZ', wildberries: 'WB', yandex: 'YM', avito: 'AV' };
  const qhash = Math.abs(query.split('').reduce((a,c) => a*31+c.charCodeAt(0), 0)) % 10000;
  return `${prefixes[pl]}-${qhash}${String(idx).padStart(4,'0')}`;
}

// ========== СКРЕПЕРЫ ПО ПЛОЩАДКАМ ==========

/**
 * OZON - поиск ключей активации
 * URL: https://www.ozon.ru/search/?text=QUERY&page=N
 * DOM: div[data-index] - карточки товаров
 */
async function scrapeOzon(page, query, maxPages) {
  const results = [];
  const baseUrl = `https://www.ozon.ru/search/?text=${encodeURIComponent(query)}&sorting=rating`;
  console.log(`  [OZON] Запрос: "${query}"`);

  for (let pageNum = 1; pageNum <= maxPages; pageNum++) {
    const url = pageNum === 1 ? baseUrl : `${baseUrl}&page=${pageNum}`;
    console.log(`    Страница ${pageNum}: ${url}`);

    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await delay();

      // Прокрутка для загрузки ленивых изображений
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight / 2));
      await sleep(800);
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await sleep(600);

      const items = await page.evaluate(() => {
        const cards = document.querySelectorAll('div[data-index]');
        const result = [];
        cards.forEach(card => {
          const text = card.innerText;
          const lines = text.split('\n').map(l => l.trim()).filter(Boolean);

          // Парсим цену (формат: "215 ₽" или "1 234 ₽")
          const priceMatch = text.match(/^([\d\s]{2,8})\s*₽/m);
          const price = priceMatch ? parseInt(priceMatch[1].replace(/\s/g, '')) : null;
          if (!price || price > 2000 || price < 10) return; // фильтр нерелевантных

          // Название - обычно длинная строка
          const title = lines.find(l => l.length > 15 && !/^\d/.test(l) && !l.includes('₽') && !l.includes('%'));

          // Ссылка на товар
          const link = card.querySelector('a[href*="/product/"]');
          const url = link ? 'https://www.ozon.ru' + link.getAttribute('href').split('?')[0] : null;

          if (title) result.push({ price, title: title.slice(0, 120), url });
        });
        return result;
      });

      if (!items.length) {
        console.log(`    Нет товаров на стр.${pageNum}, останавливаемся`);
        break;
      }

      items.forEach((item, i) => {
        if (!titleOk(item.title)) return;
        const op = detectOfficialPrice(item.title);
        results.push({
          date: today(),
          pl: 'ozon',
          id: makeId('ozon', results.length + i, query),
          query,
          title: item.title,
          url: item.url || `https://www.ozon.ru/search/?text=${encodeURIComponent(query)}`,
          price: item.price,
          op,
          regDays: null,
          auth: null,
        });
      });

      console.log(`    +${items.length} товаров (итого: ${results.length})`);
      await delay();
    } catch (e) {
      console.error(`    Ошибка на стр.${pageNum}: ${e.message}`);
      break;
    }
  }

  return results;
}

/**
 * WILDBERRIES - поиск ключей активации
 * URL: https://www.wildberries.ru/catalog/0/search.aspx?search=QUERY&page=N
 * DOM: .product-card элементы
 */
async function scrapeWildberries(page, query, maxPages) {
  const results = [];
  console.log(`  [WB] Запрос: "${query}" (DOM)`);
  const baseUrl = `https://www.wildberries.ru/catalog/0/search.aspx?search=${encodeURIComponent(query)}`;

  for (let pageNum = 1; pageNum <= maxPages; pageNum++) {
    const url = pageNum === 1 ? baseUrl : `${baseUrl}&page=${pageNum}`;
    console.log(`    Страница ${pageNum}`);

    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 40000 });
      await sleep(rand(2500, 4500));
      // Прогружаем lazy-load
      for (let s = 1; s <= 6; s++) {
        await page.evaluate(p => window.scrollTo(0, document.body.scrollHeight * p / 6), s);
        await sleep(rand(500, 900));
      }

      let items;
      try {
        items = await page.evaluate(() => {
          const out = [];
          const cards = document.querySelectorAll('article.product-card, .product-card');
          cards.forEach(card => {
            // ID карточки: data-nm-id или из href
            let id = card.getAttribute('data-nm-id');
            const a = card.querySelector('a.product-card__link, a[href*="/catalog/"]');
            const href = a ? a.getAttribute('href') : '';
            if (!id) {
              const m = (href || '').match(/\/catalog\/(\d+)\/?/);
              if (m) id = m[1];
            }
            // Заголовок: aria-label у ссылки самый чистый
            let title = a ? (a.getAttribute('aria-label') || '').trim() : '';
            if (!title) {
              const img = card.querySelector('img.j-thumbnail');
              if (img && img.alt) title = img.alt.trim();
            }
            // Цена: первое число с ₽ в innerText
            const txt = (card.innerText || '').replace(/ /g, ' ');
            const priceMatch = txt.match(/([\d ]{2,7})\s*₽/);
            const price = priceMatch ? parseInt(priceMatch[1].replace(/\s/g, '')) : null;
            if (!id || !title || !price) return;
            const url = href && href.startsWith('http') ? href.split('?')[0] : `https://www.wildberries.ru/catalog/${id}/detail.aspx`;
            out.push({ id, title, price, url });
          });
          return out;
        });
      } catch (e) {
        console.error(`    DOM ошибка: ${e.message}`);
        if (pageNum === 1) break;
        continue;
      }

      if (!items || !items.length) {
        console.log(`    Пустая страница`);
        break;
      }
      // Конвертируем под общий формат
      items = items.map(it => ({ id: it.id, price: it.price, title: it.title }));

      if (!items || !items.length) {
        console.log(`    Пустой ответ`);
        break;
      }

      let added = 0;
      items.forEach((item, i) => {
        if (!item.price || item.price < 10 || item.price > 5000) return;
        if (!item.title || item.title.length < 5) return;
        if (!titleOk(item.title)) return;
        const op = detectOfficialPrice(item.title);
        results.push({
          date: today(),
          pl: 'wildberries',
          id: `WB-${item.id}`,
          query,
          title: item.title.slice(0, 120),
          url: item.url || `https://www.wildberries.ru/catalog/${item.id}/detail.aspx`,
          price: item.price,
          op,
          regDays: null,
          auth: null,
        });
        added++;
      });

      console.log(`    +${added} товаров (всего сырых: ${items.length}, итого: ${results.length})`);
      if (items.length < 10) break;
      await delay();
    } catch (e) {
      console.error(`    Ошибка: ${e.message}`);
      break;
    }
  }

  return results;
}

/**
 * ЯНДЕКС.МАРКЕТ
 * URL: https://market.yandex.ru/search?text=QUERY&page=N
 * DOM: статьи (article) или div с data-apiary-widget
 * Внимание: ЯМ имеет защиту от ботов, используем человекоподобное поведение
 */
async function scrapeYandexMarket(page, query, maxPages) {
  const results = [];
  const baseUrl = `https://market.yandex.ru/search?text=${encodeURIComponent(query)}`;
  console.log(`  [ЯМ] Запрос: "${query}"`);

  for (let pageNum = 1; pageNum <= maxPages; pageNum++) {
    const url = pageNum === 1 ? baseUrl : `${baseUrl}&page=${pageNum}`;
    console.log(`    Страница ${pageNum}`);

    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 40000 });
      await sleep(rand(3000, 5000));
      // прокрутка для подгрузки lazy
      for (let s = 1; s <= 8; s++) {
        await page.evaluate(p => window.scrollTo(0, document.body.scrollHeight * p / 8), s);
        await sleep(rand(600, 1000));
      }

      const items = await page.evaluate(() => {
        const out = [];
        const cards = document.querySelectorAll('article[data-auto="searchOrganic"], article[id]');
        cards.forEach(card => {
          const a = card.querySelector('a[href*="/card/"], a[href*="/product"]');
          const href = a ? a.getAttribute('href') : null;
          if (!href) return;
          let id = null;
          let m = href.match(/\/card\/[^/]+\/(\d+)/);
          if (m) id = m[1];
          if (!id) { m = href.match(/\/product[^/]*\/(\d+)/); if (m) id = m[1]; }
          if (!id) return;

          const txt = (card.innerText || '').replace(/[  ]/g, ' ');
          const priceMatch = txt.match(/([\d ]{2,7})\s*₽/);
          const price = priceMatch ? parseInt(priceMatch[1].replace(/\s/g, '')) : null;
          if (!price) return;

          // Заголовок: первая «осмысленная» строка не равная цене / категории
          const lines = txt.split('\n').map(l => l.trim()).filter(Boolean);
          let title = '';
          for (const line of lines) {
            if (/^([\d ]{2,7})\s*₽/.test(line)) continue;
            if (line.length < 10) continue;
            if (/^Язык|^Категория|^Активация|^Количество|^Цена|^Бесплатная|^Доставка|^Купили|^Спонсор|^Рейтинг|^В корз/i.test(line)) continue;
            title = line;
            break;
          }
          if (!title) return;
          const cleanHref = href.split('?')[0];
          out.push({ id, title: title.slice(0, 120), price, url: `https://market.yandex.ru${cleanHref}` });
        });
        return out;
      });

      // Дедупликация по id внутри страницы
      const seen = new Set();
      const unique = items.filter(item => {
        if (seen.has(item.id)) return false;
        seen.add(item.id);
        return true;
      });

      if (!unique.length) {
        console.log(`    Нет товаров на стр.${pageNum}`);
        break;
      }

      let added = 0;
      unique.forEach((item) => {
        if (!item.price || item.price < 10 || item.price > 5000) return;
        if (!titleOk(item.title)) return;
        const op = detectOfficialPrice(item.title);
        results.push({
          date: today(),
          pl: 'yandex',
          id: `YM-${item.id}`,
          query,
          title: item.title,
          url: item.url,
          price: item.price,
          op,
          regDays: null,
          auth: null,
        });
        added++;
      });

      console.log(`    +${added} товаров (сырых ${unique.length}, итого: ${results.length})`);
      await delay();
    } catch (e) {
      console.error(`    Ошибка: ${e.message}`);
      break;
    }
  }

  return results;
}

/**
 * АВИТО
 * URL: https://www.avito.ru/rossiya/tovary/... или поиск
 * DOM: div[data-marker="item"] - карточки объявлений
 */
async function scrapeAvito(page, query, maxPages) {
  const results = [];
  const baseUrl = `https://www.avito.ru/rossiya?q=${encodeURIComponent(query)}&s=104`;  // s=104 = по цене
  console.log(`  [Авито] Запрос: "${query}"`);

  for (let pageNum = 1; pageNum <= maxPages; pageNum++) {
    const url = pageNum === 1 ? baseUrl : `${baseUrl}&p=${pageNum}`;
    console.log(`    Страница ${pageNum}`);

    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await sleep(rand(1500, 3000));
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight / 2));
      await sleep(700);

      const items = await page.evaluate(() => {
        const cards = document.querySelectorAll('[data-marker="item"]');
        const result = [];
        cards.forEach(card => {
          const titleEl = card.querySelector('[itemprop="name"]') || card.querySelector('h3');
          const priceEl = card.querySelector('[data-marker="item-price"]') || card.querySelector('.price-text');
          const linkEl = card.querySelector('a[data-marker="item-title"]') || card.querySelector('a[href*="/tovary"]') || card.querySelector('a');

          const title = titleEl?.innerText?.trim();
          const priceText = priceEl?.innerText || '';
          const price = parseInt(priceText.replace(/\D/g, ''));
          const href = linkEl?.href;

          if (!price || !title || price > 5000 || price < 10) return;
          if (!title.toLowerCase().match(/office|windows|microsoft|ключ|лиценз/)) return;
          result.push({ price, title: title.slice(0, 120), url: href || null });
        });
        return result;
      });

      if (!items.length) {
        console.log(`    Нет товаров на стр.${pageNum}`);
        break;
      }

      items.forEach((item, i) => {
        if (!titleOk(item.title)) return;
        const op = detectOfficialPrice(item.title);
        results.push({
          date: today(),
          pl: 'avito',
          id: makeId('avito', results.length + i, query),
          query,
          title: item.title,
          url: item.url || `https://www.avito.ru/rossiya?q=${encodeURIComponent(query)}`,
          price: item.price,
          op,
          regDays: null,
          auth: null,
        });
      });

      console.log(`    +${items.length} товаров (итого: ${results.length})`);
      await delay();
    } catch (e) {
      console.error(`    Ошибка: ${e.message}`);
      break;
    }
  }

  return results;
}

// ========== ГЛАВНЫЙ ЗАПУСК ==========
const SCRAPERS = { ozon: scrapeOzon, wildberries: scrapeWildberries, yandex: scrapeYandexMarket, avito: scrapeAvito };

async function run() {
  console.log('='.repeat(60));
  console.log('Microsoft Key Monitor - Scraper v1.0');
  console.log(`Дата: ${today()}`);
  console.log(`Площадки: ${CONFIG.platforms.join(', ')}`);
  console.log(`Запросы (${CONFIG.queries.length}): ${CONFIG.queries.map(q => `"${q}"`).join(', ')}`);
  console.log(`Страниц на площадку: ${CONFIG.maxPages}`);
  console.log('='.repeat(60));

  const launchOptions = {
    headless: CONFIG.headless,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--lang=ru-RU',
    ],
  };
  if (CONFIG.proxy) {
    launchOptions.proxy = { server: CONFIG.proxy };
    console.log(`Прокси: ${CONFIG.proxy}`);
  }
  const browser = await chromium.launch(launchOptions);

  // Эмулируем обычный браузер
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    viewport: { width: 1366, height: 768 },
    extraHTTPHeaders: { 'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8' },
  });

  // Скрываем признаки автоматизации
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
  });

  const page = await context.newPage();
  const allResults = [];
  const stats = {};

  for (const query of CONFIG.queries) {
    console.log(`\n>>> Запрос: "${query}"`);
    stats[query] = {};

    for (const platform of CONFIG.platforms) {
      const scraper = SCRAPERS[platform];
      if (!scraper) { console.log(`  Нет скрепера для ${platform}`); continue; }

      try {
        const items = await scraper(page, query, CONFIG.maxPages);
        allResults.push(...items);
        stats[query][platform] = items.length;
        console.log(`  ${platform}: ${items.length} позиций`);
      } catch (e) {
        console.error(`  Ошибка ${platform}: ${e.message}`);
        stats[query][platform] = 0;
      }

      // Пауза между площадками
      await sleep(rand(2000, 5000));
    }
  }

  await browser.close();

  // Добавляем флажки к каждой записи
  const withFlags = allResults.map(r => ({ ...r, flags: computeFlags(r) }));

  // Сортируем по убыванию дисконта
  withFlags.sort((a, b) => b.flags.disc - a.flags.disc);

  // ========== ВЫВОД СТАТИСТИКИ ==========
  console.log('\n' + '='.repeat(60));
  console.log(`ИТОГО: ${withFlags.length} позиций`);
  for (const [q, pls] of Object.entries(stats)) {
    console.log(`  "${q}": ${Object.entries(pls).map(([pl,n]) => `${pl}=${n}`).join(', ')}`);
  }

  const f1 = withFlags.filter(r => r.flags.F1).length;
  const f2 = withFlags.filter(r => r.flags.F2).length;
  console.log(`Флажок F1 (скидка >50%): ${f1}`);
  console.log(`Флажок F2 (скидка >80%): ${f2}`);

  // Топ-5 по дисконту
  console.log('\nТоп-5 по дисконту:');
  withFlags.slice(0, 5).forEach(r => {
    console.log(`  [${r.flags.disc}%] ${r.pl.padEnd(12)} ${r.price}₽ - ${r.title.slice(0,60)}`);
  });

  // ========== СОХРАНЕНИЕ ==========
  // Формат mon_data.json совместим с MON_RAW в дашборде
  const output = {
    generated: new Date().toISOString(),
    queries: CONFIG.queries,
    total: withFlags.length,
    stats,
    data: withFlags,
  };

  fs.writeFileSync(CONFIG.outputFile, JSON.stringify(output, null, 2), 'utf8');
  console.log(`\nСохранено: ${CONFIG.outputFile}`);

  // Также сохраняем только массив данных для вставки в MON_RAW дашборда
  const monRawJs = '// Автосгенерировано: ' + new Date().toISOString() + '\n'
    + 'const MON_RAW = ' + JSON.stringify(withFlags.map(r => {
        const { flags, ...rest } = r; // убираем флажки - они пересчитаются
        return rest;
      }), null, 2) + ';\n';

  fs.writeFileSync(
    path.join(path.dirname(CONFIG.outputFile), 'mon_raw_snippet.js'),
    monRawJs, 'utf8'
  );
  console.log(`JS-сниппет для вставки в дашборд: ${path.join(path.dirname(CONFIG.outputFile), 'mon_raw_snippet.js')}`);
  console.log('='.repeat(60));
}

run().catch(e => {
  console.error('FATAL:', e);
  process.exit(1);
});

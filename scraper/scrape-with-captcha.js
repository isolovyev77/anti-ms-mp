#!/usr/bin/env node
/**
 * Headful-парсер для Ozon и Avito.
 *
 * Headless Playwright ловит капчу/блок IP даже когда обычный Chrome пускает —
 * antibot читает webdriver/fingerprint. Этот скрипт открывает видимый Chromium
 * с persistent-контекстом: вы один раз решаете капчу в окне, дальше парсинг
 * идёт автоматически. Куки и localStorage сохраняются между запусками в
 * `~/.cache/anti-ms-mp/chromium-profile/`, поэтому капчу обычно достаточно
 * пройти один раз.
 *
 * Использование:
 *   node scrape-with-captcha.js                       # ozon + avito, 5 страниц
 *   node scrape-with-captcha.js --platforms ozon --pages 10
 *
 * Параметры:
 *   --platforms ozon,avito   список площадок (по умолчанию обе)
 *   --pages N                страниц на запрос (5)
 *   --output FILE            куда писать mon_data.json
 *   --proxy URL              SOCKS5/HTTP прокси (можно SCRAPER_PROXY env)
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const os = require('os');

const CONFIG = {
  queries: [
    'Microsoft Office ключ активации',
    'Microsoft Office 365 ключ',
    'Office 2021 ключ активации',
    'Office 2024 ключ активации',
    'MS Office ключ активации',
  ],
  platforms: ['ozon', 'avito'],
  maxPages: 5,
  delay: { min: 1500, max: 3500 },
  outputFile: path.join(__dirname, 'mon_data_captcha.json'),
  profileDir: path.join(os.homedir(), '.cache', 'anti-ms-mp', 'chromium-profile'),
  proxy: process.env.SCRAPER_PROXY || null,
  officialPrices: {
    office365: 6990,
    office2021home: 14990,
    office2021homebus: 22990,
    default: 9990,
  },
};

const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--platforms' && args[i + 1]) { CONFIG.platforms = args[i + 1].split(','); i++; }
  if (args[i] === '--pages' && args[i + 1]) { CONFIG.maxPages = parseInt(args[i + 1]); i++; }
  if (args[i] === '--output' && args[i + 1]) { CONFIG.outputFile = args[i + 1]; i++; }
  if (args[i] === '--proxy' && args[i + 1]) { CONFIG.proxy = args[i + 1]; i++; }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;
const delay = () => sleep(rand(CONFIG.delay.min, CONFIG.delay.max));
const today = () => new Date().toISOString().slice(0, 10);

function titleOk(title) {
  const t = title.toLowerCase();
  if (t.includes('officesuite')) return false;
  return (
    t.includes('office') ||
    (t.includes('365') && t.includes('microsoft')) ||
    (t.includes('офис') && t.includes('microsoft'))
  );
}

function detectOfficialPrice(title) {
  const t = title.toLowerCase();
  if (t.includes('365') || t.includes('personal')) return CONFIG.officialPrices.office365;
  if (t.includes('home and business') || t.includes('hb')) return CONFIG.officialPrices.office2021homebus;
  if (t.includes('2021') || t.includes('2024') || t.includes('2019')) return CONFIG.officialPrices.office2021home;
  return CONFIG.officialPrices.default;
}

async function waitUntilHuman(page, marker) {
  // Если в title или body есть «капча/доступ ограничен» — ждём пока пользователь решит.
  const looksBlocked = async () => {
    const title = await page.title();
    const body = await page.evaluate(() => document.body && document.body.innerText.slice(0, 300) || '');
    return /Antibot|Доступ ограничен|капч|captcha|Подтвердите/i.test(title + ' ' + body);
  };
  if (!(await looksBlocked())) return;
  console.log(`\n⚠️  ${marker}: похоже, площадка показывает капчу.`);
  console.log('    Решите капчу в открытом окне браузера — скрипт продолжит автоматически.\n');
  const startedAt = Date.now();
  while (Date.now() - startedAt < 300_000) { // ждём до 5 минут
    await sleep(2000);
    if (!(await looksBlocked())) {
      console.log('    ✓ Капча пройдена, продолжаем\n');
      return;
    }
  }
  throw new Error(`Капча на ${marker} не пройдена за 5 минут`);
}

async function scrapeOzon(page, query) {
  const results = [];
  console.log(`  [OZON] "${query}"`);
  for (let p = 1; p <= CONFIG.maxPages; p++) {
    const url = `https://www.ozon.ru/search/?text=${encodeURIComponent(query)}&from_global=true&page=${p}`;
    console.log(`    стр.${p}`);
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 35000 });
      await sleep(rand(2000, 3500));
      await waitUntilHuman(page, `OZON стр.${p}`);
      // Lazy-load прокрутка
      for (let s = 1; s <= 10; s++) {
        await page.evaluate(k => window.scrollTo(0, document.body.scrollHeight * k / 10), s);
        await sleep(rand(350, 600));
      }
      const items = await page.evaluate(() => {
        const out = [];
        const seen = new Set();
        const links = document.querySelectorAll('a[href*="/product/"]');
        for (const a of links) {
          const href = a.getAttribute('href') || '';
          const m = href.match(/-(\d{5,12})\/?/);
          if (!m) continue;
          const id = m[1];
          if (seen.has(id)) continue;
          seen.add(id);
          // Контейнер карточки
          let card = a.closest('div[data-widget="searchResultsV2"] > div');
          if (!card) card = a.closest('[data-index]');
          if (!card) card = a.parentElement;
          const text = card ? card.innerText : a.innerText;
          // Заголовок: самая длинная строка без ₽/% в первых 8
          const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
          let title = '';
          for (const line of lines.slice(0, 10)) {
            if (/[₽%]/.test(line)) continue;
            if (line.length > title.length && line.length > 15) title = line;
          }
          if (!title) continue;
          // Цена: первое число с ₽
          const pm = text.replace(/ /g, ' ').match(/([\d ]{2,7})\s*₽/);
          const price = pm ? parseInt(pm[1].replace(/\s/g, '')) : null;
          if (!price) continue;
          out.push({ id, title: title.slice(0, 120), price, url: `https://www.ozon.ru${href.split('?')[0]}` });
        }
        return out;
      });
      let added = 0;
      items.forEach(it => {
        if (it.price < 10 || it.price > 5000) return;
        if (!titleOk(it.title)) return;
        const op = detectOfficialPrice(it.title);
        results.push({
          date: today(), pl: 'ozon', id: `OZ-${it.id}`, query,
          title: it.title, url: it.url, price: it.price, op,
          regDays: null, auth: null,
        });
        added++;
      });
      console.log(`      +${added} (всего сырых: ${items.length})`);
      if (items.length === 0) break;
      await delay();
    } catch (e) {
      console.error(`      ошибка: ${e.message}`);
      break;
    }
  }
  return results;
}

async function scrapeAvito(page, query) {
  const results = [];
  console.log(`  [AVITO] "${query}"`);
  const base = `https://www.avito.ru/rossiya/igry_pristavki_i_programmy/programmy-ASgBAgICAUSSAs4J?q=${encodeURIComponent(query)}`;
  for (let p = 1; p <= CONFIG.maxPages; p++) {
    const url = p === 1 ? base : `${base}&p=${p}`;
    console.log(`    стр.${p}`);
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 35000 });
      await sleep(rand(2000, 3500));
      await waitUntilHuman(page, `AVITO стр.${p}`);
      for (let s = 1; s <= 8; s++) {
        await page.evaluate(k => window.scrollTo(0, document.body.scrollHeight * k / 8), s);
        await sleep(rand(400, 700));
      }
      const items = await page.evaluate(() => {
        const out = [];
        const cards = document.querySelectorAll('[data-marker="item"]');
        cards.forEach(card => {
          const id = card.getAttribute('data-item-id');
          if (!id) return;
          const titleEl = card.querySelector('[itemprop="name"], h3');
          const priceEl = card.querySelector('[data-marker="item-price"], [class*="price-root"]');
          const linkEl = card.querySelector('a[data-marker="item-title"], a[href*="/item"]');
          const title = (titleEl && titleEl.innerText || '').trim();
          const priceText = priceEl ? priceEl.innerText : '';
          const price = parseInt((priceText || '').replace(/\D/g, ''));
          const href = linkEl ? linkEl.href : `https://www.avito.ru/items/${id}`;
          if (!title || !price) return;
          out.push({ id, title: title.slice(0, 120), price, url: href.split('?')[0] });
        });
        return out;
      });
      let added = 0;
      items.forEach(it => {
        if (it.price < 10 || it.price > 5000) return;
        if (!titleOk(it.title)) return;
        const op = detectOfficialPrice(it.title);
        results.push({
          date: today(), pl: 'avito', id: `AV-${it.id}`, query,
          title: it.title, url: it.url, price: it.price, op,
          regDays: null, auth: null,
        });
        added++;
      });
      console.log(`      +${added} (всего сырых: ${items.length})`);
      if (items.length === 0) break;
      await delay();
    } catch (e) {
      console.error(`      ошибка: ${e.message}`);
      break;
    }
  }
  return results;
}

const SCRAPERS = { ozon: scrapeOzon, avito: scrapeAvito };

(async () => {
  console.log('='.repeat(60));
  console.log('Headful scraper (с ручным решением капчи)');
  console.log(`Профиль: ${CONFIG.profileDir}`);
  console.log(`Площадки: ${CONFIG.platforms.join(', ')}`);
  console.log(`Запросов: ${CONFIG.queries.length} × ${CONFIG.maxPages} страниц`);
  if (CONFIG.proxy) console.log(`Прокси: ${CONFIG.proxy}`);
  console.log('='.repeat(60));

  fs.mkdirSync(CONFIG.profileDir, { recursive: true });

  const launchOpts = {
    headless: false,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--lang=ru-RU',
      '--no-default-browser-check',
    ],
    viewport: { width: 1366, height: 768 },
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  };
  if (CONFIG.proxy) launchOpts.proxy = { server: CONFIG.proxy };

  const context = await chromium.launchPersistentContext(CONFIG.profileDir, launchOpts);
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = context.pages()[0] || await context.newPage();

  const all = [];
  const stats = {};
  for (const query of CONFIG.queries) {
    console.log(`\n>>> "${query}"`);
    stats[query] = {};
    for (const pl of CONFIG.platforms) {
      const fn = SCRAPERS[pl];
      if (!fn) continue;
      try {
        const items = await fn(page, query);
        all.push(...items);
        stats[query][pl] = items.length;
      } catch (e) {
        console.error(`  ${pl} fatal: ${e.message}`);
        stats[query][pl] = 0;
      }
      await sleep(rand(2000, 4000));
    }
  }

  await context.close();

  // Дедупликация по (pl, id)
  const byKey = new Map();
  for (const r of all) byKey.set(`${r.pl}|${r.id}`, r);
  const dedup = Array.from(byKey.values());

  console.log('\n' + '='.repeat(60));
  console.log(`Сырых: ${all.length}, после dedup: ${dedup.length}`);
  for (const [q, pls] of Object.entries(stats)) {
    console.log(`  "${q}": ${Object.entries(pls).map(([p, n]) => `${p}=${n}`).join(', ')}`);
  }

  fs.writeFileSync(CONFIG.outputFile, JSON.stringify({
    generated: new Date().toISOString(),
    queries: CONFIG.queries,
    total: dedup.length,
    stats,
    data: dedup,
  }, null, 2), 'utf8');
  console.log(`\nСохранено: ${CONFIG.outputFile}`);
})().catch(e => { console.error('FATAL', e); process.exit(1); });

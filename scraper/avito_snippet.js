/* ============================================================
 * AVITO collector — запускать в DevTools Yandex Browser
 * (Chrome/Safari ловят capчу-IP, Yandex Browser — нет)
 *
 * Использование:
 *   1) Откройте https://www.avito.ru/rossiya?q=Microsoft+Office+ключ в Yandex Browser
 *      (любая страница avito.ru, главное — что капча пройдена и cookies есть)
 *   2) Откройте DevTools (Cmd+Opt+I) → вкладка Console
 *   3) Вставьте весь этот скрипт целиком, Enter
 *   4) Скрипт пройдёт 5 запросов × до 10 страниц, в конце скачает avito_2026-05-26.json
 *   5) Файл лежит в ~/Downloads — передайте мне, я добавлю в дашборд
 *
 * Сколько ждать: ~3-5 минут (50 запросов × ~3 сек/страница)
 * Не закрывайте вкладку, прогресс пишется в console.
 * ============================================================ */
(async () => {
  const QUERIES = [
    'Microsoft Office ключ активации',
    'Microsoft Office 365 ключ',
    'Office 2021 ключ активации',
    'Office 2024 ключ активации',
    'MS Office ключ активации',
  ];
  const MAX_PAGES = 10;

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const all = [];
  const stats = {};

  // Avito SSR'ит карточки в HTML. Парсим через DOMParser.
  function parseHtml(html, query) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const out = [];
    const cards = doc.querySelectorAll('[data-marker="item"]');
    cards.forEach(c => {
      const id = c.getAttribute('data-item-id');
      if (!id) return;
      const titleEl = c.querySelector('h3, [itemprop="name"]');
      const title = titleEl ? titleEl.textContent.trim().replace(/\s+/g, ' ').slice(0, 120) : '';
      if (!title || title.length < 5) return;
      // цена: ищем элемент с symbol ₽
      const priceCandidates = c.querySelectorAll('[itemprop="price"], [class*="price-root"], [class*="Price"], [data-marker="item-price"]');
      let price = null;
      for (const p of priceCandidates) {
        const t = (p.getAttribute('content') || p.textContent || '').replace(/[   ]/g, ' ');
        const m = t.match(/(\d[\d\s]{0,6})/);
        if (m) { const n = parseInt(m[1].replace(/\s/g, '')); if (n && n > 5 && n < 100000) { price = n; break; } }
      }
      if (!price) return;
      const linkEl = c.querySelector('a[data-marker="item-title"], a[href*="/item"], a[href*="_"]');
      const href = linkEl ? linkEl.getAttribute('href') : `/items/${id}`;
      const url = href.startsWith('http') ? href : 'https://www.avito.ru' + href;
      out.push({ id, title, price, url: url.split('?')[0], query });
    });
    return out;
  }

  for (const q of QUERIES) {
    console.log(`\n>>> ${q}`);
    stats[q] = 0;
    let prevFirstId = null;
    for (let p = 1; p <= MAX_PAGES; p++) {
      const url = `https://www.avito.ru/rossiya?q=${encodeURIComponent(q)}&s=104${p > 1 ? `&p=${p}` : ''}`;
      try {
        const r = await fetch(url, { credentials: 'include', headers: { 'Accept': 'text/html', 'User-Agent': navigator.userAgent } });
        if (!r.ok) { console.warn(`  стр.${p}: HTTP ${r.status}`); break; }
        const html = await r.text();
        if (/Доступ ограничен|капч|captcha/i.test(html.slice(0, 1000))) {
          console.warn(`  стр.${p}: блок IP в ответе — обновите страницу руками, потом запустите снова`);
          break;
        }
        const items = parseHtml(html, q);
        if (!items.length) { console.log(`  стр.${p}: пусто, конец`); break; }
        if (prevFirstId && items[0] && items[0].id === prevFirstId) {
          console.log(`  стр.${p}: дубль первой карточки — конец`);
          break;
        }
        prevFirstId = items[0] && items[0].id;
        all.push(...items);
        stats[q] += items.length;
        console.log(`  стр.${p}: +${items.length} (всего ${all.length})`);
        await sleep(800 + Math.floor(Math.random() * 700)); // 0.8-1.5 сек между запросами
      } catch (e) {
        console.warn(`  стр.${p}: ошибка ${e.message}`);
        break;
      }
    }
  }

  // Дедуп по id
  const seen = new Set();
  const dedup = all.filter(x => { if (seen.has(x.id)) return false; seen.add(x.id); return true; });

  // Фильтр titleOk: только Office
  function titleOk(t) {
    const s = t.toLowerCase();
    if (s.includes('officesuite')) return false;
    if (/^код windows|^ключ windows|^windows\s+\d/.test(s)) return false;
    return s.includes('office') || (s.includes('365') && s.includes('microsoft')) || (s.includes('офис') && s.includes('microsoft'));
  }
  const filtered = dedup.filter(x => titleOk(x.title));

  console.log(`\n========== ИТОГО ==========`);
  console.log(`Сырых: ${all.length}, dedup: ${dedup.length}, после titleOk: ${filtered.length}`);
  console.log('Stats:', stats);

  // Скачиваем результат
  const payload = {
    generated: new Date().toISOString(),
    source: 'avito-yandex-browser-amneziavpn',
    total: filtered.length,
    queries: QUERIES,
    items: filtered,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const dlUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = dlUrl;
  a.download = `avito_${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(dlUrl); }, 1000);
  console.log(`✓ Скачано: ${a.download} (${filtered.length} карточек)`);
})();

#!/usr/bin/env python3
"""Общие экстракторы/фильтры anti-ms-mp — единый источник для обоих движков.

Импортируют и боевой парсер (parser_runner.py, CloakBrowser/Chromium), и
запасной движок (camoufox_scrape.py, Firefox). НИКАКИХ зависимостей от
cloakbrowser/supabase — только стандартная библиотека, чтобы модуль грузился
в любом venv. JS из EXTRACT_JS одинаково исполняется в page.evaluate и в
Chromium, и в Firefox (проверено на Яндексе).

⚠️ СИНХРОНИЗАЦИЯ: parser_runner.py исторически держит СВОИ копии этих же
EXTRACT_JS/PLATFORMS/title_ok/official_price. При смене вёрстки маркетплейса
или правке фильтра — менять В ОБОИХ файлах. (TODO: перевести parser_runner на
`from extractors import ...` отдельным рефактором с полным прогоном-тестом —
тогда дубль уйдёт и это станет единственным источником.)
"""
import re as _re
from urllib.parse import quote

PLATFORMS = {
    "ozon": {
        "url": lambda q, p: f"https://www.ozon.ru/search/?text={quote(q)}&sorting=rating&page={p}",
        "max_pages": 5,
        "card_selector": "div[data-index]",
    },
    "wildberries": {
        "url": lambda q, p: f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote(q)}&page={p}",
        "max_pages": 5,
        "card_selector": "article.product-card",
    },
    "yandex": {
        "url": lambda q, p: f"https://market.yandex.ru/search?text={quote(q)}&page={p}",
        "max_pages": 5,
        "card_selector": 'article[data-auto="searchOrganic"]',
    },
    "avito": {
        "url": lambda q, p: f"https://www.avito.ru/rossiya?q={quote(q)}&s=104"
                            + (f"&p={p}" if p > 1 else ""),
        "max_pages": 5,
        "card_selector": '[data-marker="item"]',
    },
}

OFFICIAL_PRICES = {
    "m365_personal":       2790,
    "m365_family":         5290,
    "office365_box":       6990,
    "office_home_student": 14990,
    "office_home_bus":     22990,
    "office_pro":          39990,
    "default":             9990,
}

BLOCK_MARKERS = ["datadome", "доступ ограничен", "captcha", "challenge",
                 "запросов с вашего ip", "возможно, что-то пошло не так"]

EXTRACT_JS = {
    "ozon": r"""
    () => {
      const out = [];
      document.querySelectorAll('div[data-index]').forEach(card => {
        const link = card.querySelector('a[href*="/product/"]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        const m = href.match(/\/product\/[^\/?#]*?-(\d{6,12})/);
        if (!m) return;
        let title = '';
        card.querySelectorAll('span').forEach(s => {
          const t = (s.textContent || '').trim();
          if (t.length > 10 && t.length > title.length && t.length < 250) title = t;
        });
        let price = 0;
        card.querySelectorAll('span, div').forEach(el => {
          const t = (el.textContent || '').trim();
          if (/^\d[\d\s]*\s*₽$/.test(t)) {
            const n = parseInt(t.replace(/\D/g, ''), 10);
            if (n && (!price || n < price)) price = n;
          }
        });
        out.push({ id: m[1], title, price, url: 'https://www.ozon.ru' + href.split('?')[0] });
      });
      return out;
    }""",
    "avito": r"""
    () => {
      const out = [];
      document.querySelectorAll('[data-marker="item"]').forEach(card => {
        const link = card.querySelector('a[data-marker="item-title"]') || card.querySelector('a[href*="_"]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        const m = href.match(/_(\d{7,12})(?:[/?#]|$)/);
        if (!m) return;
        const titleEl = card.querySelector('[itemprop="name"]') || card.querySelector('h3');
        const title = (titleEl ? titleEl.textContent : link.textContent || '').trim();
        const priceEl = card.querySelector('[data-marker="item-price"]') || card.querySelector('[itemprop="price"]');
        let price = 0;
        if (priceEl) {
          const raw = (priceEl.textContent || '').trim();
          const n = raw ? parseInt(raw.replace(/\D/g, ''), 10)
                        : Math.round(parseFloat(priceEl.getAttribute('content') || '0'));
          if (n) price = n;
        }
        const url = href.startsWith('http') ? href : 'https://www.avito.ru' + href;
        out.push({ id: m[1], title: title.slice(0, 200), price, url });
      });
      return out;
    }""",
    "wildberries": r"""
    () => {
      const out = [];
      document.querySelectorAll('article.product-card').forEach(card => {
        const link = card.querySelector('a.product-card__link') || card.querySelector('a[href*="/catalog/"]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        const m = href.match(/\/catalog\/(\d+)\/detail/);
        if (!m) return;
        const id = m[1] || card.getAttribute('data-nm-id');
        if (!id) return;
        const title = (card.querySelector('.product-card__name')?.textContent || link.getAttribute('aria-label') || '').trim();
        const priceRaw = card.querySelector('.price__lower-price')?.textContent || card.querySelector('ins')?.textContent || '';
        const priceText = priceRaw.replace(/[  ]/g, ' ');
        const price = parseInt(priceText.replace(/\D/g, ''), 10) || 0;
        out.push({ id: String(id), title, price, url: href.startsWith('http') ? href : 'https://www.wildberries.ru' + href });
      });
      return out;
    }""",
    "yandex": r"""
    () => {
      const out = [];
      document.querySelectorAll('article[data-auto="searchOrganic"]').forEach(card => {
        const innerStart = (card.innerText || '').slice(0, 60);
        if (innerStart.startsWith('(window.') || innerStart.startsWith('apiary') || innerStart.startsWith('{"widgets"')) return;

        const link = card.querySelector('a[data-auto="snippet-link"]') || card.querySelector('a[href*="/card/"]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        const m = href.match(/\/card\/[^\/]+\/(\d+)/);
        if (!m) return;
        const title = (card.querySelector('[data-auto="snippet-title"]')?.textContent || link.textContent || '').trim();

        const norm = s => (s || '').replace(/[  ]/g, ' ');
        let price = 0;
        for (const sel of ['[data-auto="snippet-price"]', '[data-auto="price-value"]', '[data-auto="mainPrice"]']) {
          const el = card.querySelector(sel);
          if (!el) continue;
          const m = norm(el.textContent).match(/(\d[\d\s]{0,12})\s*₽/);
          if (m) { const n = parseInt(m[1].replace(/\D/g, ''), 10); if (n > 0) { price = n; break; } }
        }
        if (!price) {
          for (const el of card.querySelectorAll('*')) {
            if (el.children.length > 1) continue;
            const m = norm(el.textContent).trim().match(/^(\d[\d\s]{0,12})\s*₽/);
            if (m) { const n = parseInt(m[1].replace(/\D/g, ''), 10); if (n > 0) { price = n; break; } }
          }
        }

        out.push({ id: m[1], title: title.slice(0, 200), price, url: href.startsWith('http') ? href : 'https://market.yandex.ru' + href });
      });
      return out;
    }""",
}


def title_ok(title: str) -> bool:
    """Фильтр контрафакта MS Office (точная копия из parser_runner.py)."""
    s = (title or "").lower()
    if "officesuite" in s:
        return False
    if any(x in s for x in ("р7-", "р7 офис", "мойофис", "redos", "ред ос",
                            "libreoffice", "astra linux", "базальт", "rosa",
                            "кит офис", "обычный офис")):
        return False
    if _re.search(r"\bxbox\b|playstation|\bps[2345]\b|nintendo|game\s?pass|gamepass", s):
        return False
    if _re.search(r"\bкнига\b|\bруководство\b|учебник|учебн[ао]е|пособи[ея]|\bсамоучитель\b|шаг за шагом|методичк|методическ|монограф|\bлекци[ия]\b|power bi", s):
        return False
    # Услуги ПК-ремонта (установка Windows/драйверов, лечение вирусов, восстановление,
    # очистка) и антивирусы — это не продажа MS Office, даже если «office» в названии.
    # «Установка Office» с ценой остаётся — этих сервис-маркеров в ней нет.
    if _re.search(r"драйвер|вирус|лечени|восстановлени|очистк[аи]|\bремонт", s):
        return False
    if _re.match(r"^код windows|^ключ windows|^windows\s+\d|^лицензия windows", s):
        return False
    if _re.match(r"^office suite|^офисный пакет(?! microsoft)", s):
        return False
    brand_hw = _re.search(r"vivobook|ideapad|thinkbook|thinkpad|magicbook|matebook|"
                          r"aspire\s+go|ozon-?book|ninkear|super-?book", s)
    generic_hw = (_re.search(r"\bноутбук\b|\bnotebook\b|\blaptop\b|моноблок|неттоп|системный блок", s)
                  and not _re.search(r"для\s+(ноутбук|notebook|laptop|macbook|план|пк|компьютер|устройств)", s))
    if brand_hw or generic_hw:
        return False
    return (
        "office" in s
        or ("365" in s and ("microsoft" in s or "ms" in s.split()))
        or ("офис" in s and "microsoft" in s)
    )


def official_price(title: str) -> int:
    """Подбор офиц. цены Microsoft по типу продукта (копия из parser_runner.py)."""
    t = (title or "").lower()
    if any(k in t for k in ("pro plus", "professional plus", "pro+", "ltsc")):
        return OFFICIAL_PRICES["office_pro"]
    if "family" in t or "семь" in t or "для семьи" in t or "family pack" in t:
        return OFFICIAL_PRICES["m365_family"]
    if "personal" in t or "персональн" in t or ("m365" in t.replace(" ", "")) or "1тб onedrive" in t.replace(" ", ""):
        return OFFICIAL_PRICES["m365_personal"]
    if "home and business" in t or "home & business" in t or "для работы" in t or " h&b" in t:
        return OFFICIAL_PRICES["office_home_bus"]
    if "home and student" in t or "home & student" in t or "для дома и учёбы" in t or "для дома и учебы" in t:
        return OFFICIAL_PRICES["office_home_student"]
    if "365" in t and ("office" in t or "microsoft" in t):
        return OFFICIAL_PRICES["office365_box"]
    if any(y in t for y in ("2024", "2021", "2019", "2016")):
        return OFFICIAL_PRICES["office_home_student"]
    return OFFICIAL_PRICES["default"]

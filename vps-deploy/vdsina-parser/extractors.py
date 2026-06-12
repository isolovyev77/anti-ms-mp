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
          // Если textContent пуст — это <meta itemprop="price" content="299.00">:
          // парсим как ДРОБНОЕ (parseFloat), иначе replace(/\D/g,'') срезал бы точку
          // и «299.00» превратилось бы в 29900 (цена ×100, мимо counterfeit-детекции).
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
        // NBSP-разделитель тысяч на WB: "2 021 ₽" → парсим в 2021 а не 21
        const priceRaw = card.querySelector('.price__lower-price')?.textContent || card.querySelector('ins')?.textContent || '';
        const priceText = priceRaw.replace(/[  ]/g, ' ');
        const price = parseInt(priceText.replace(/\D/g, ''), 10) || 0;
        out.push({ id: String(id), title, price, url: href.startsWith('http') ? href : 'https://www.wildberries.ru' + href });
      });
      return out;
    }""",
    "yandex": r"""
    () => {
      const out = [];
      document.querySelectorAll('article[data-auto="searchOrganic"]').forEach(card => {
        // Skip lazy-load placeholders (карточки содержат JS-код apiary до прокрутки)
        const innerStart = (card.innerText || '').slice(0, 60);
        if (innerStart.startsWith('(window.') || innerStart.startsWith('apiary') || innerStart.startsWith('{"widgets"')) return;

        const link = card.querySelector('a[data-auto="snippet-link"]') || card.querySelector('a[href*="/card/"]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        const m = href.match(/\/card\/[^\/]+\/(\d+)/);
        if (!m) return;
        const title = (card.querySelector('[data-auto="snippet-title"]')?.textContent || link.textContent || '').trim();

        // Цена — берём ту, что показана покупателю. Яндекс сменил вёрстку:
        // data-auto-селекторы цены мертвы, классы обфусцированы (_26ABJ, ds-text).
        // Цена теперь в листовом элементе с текстом вида "72 ₽Пэй" (число + ₽ +
        // ярлык Yandex Pay). Требуем, чтобы текст НАЧИНАЛСЯ с числа перед ₽ —
        // якорь ^ отсекает "от 72 ₽" (рассрочка), "+5 ₽" (кешбэк), рейтинги без валюты.
        // Низкие цены (14₽, 22₽) НЕ отсекаем — это РЕАЛЬНЫЙ контрафакт.
        // [\d\s] (а не [\d ]) — чтобы ловить разделитель тысяч в "11 602 ₽" (там спец-
        // пробел  / , а не обычный): без этого цены ≥1000₽ не извлекались.
        const norm = s => (s || '').replace(/[  ]/g, ' ');
        let price = 0;
        // 1) старые data-auto (вдруг для части карточек ещё живы)
        for (const sel of ['[data-auto="snippet-price"]', '[data-auto="price-value"]', '[data-auto="mainPrice"]']) {
          const el = card.querySelector(sel);
          if (!el) continue;
          const m = norm(el.textContent).match(/(\d[\d\s]{0,12})\s*₽/);
          if (m) { const n = parseInt(m[1].replace(/\D/g, ''), 10); if (n > 0) { price = n; break; } }
        }
        // 2) первый листовой элемент, чей текст начинается с "<число> ₽"
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


def title_ok(title: str, url: str = "") -> bool:
    """Фильтр контрафакта MS Office.

    Книжные и «никогда-не-Office» мерч-маркеры (учебник/cookbook/календарь/кружка…) —
    по заголовку+слагу, безопасны на всех площадках. Агрессивная anti-bleed защита
    (Ozon кладёт в один div[data-index] чужие span'ы, заголовок «налипает» с соседа) —
    ТОЛЬКО для Ozon: его слаг = имя товара, поэтому если Ozon-слаг не про Office
    (флешка/photoshop/очки) — карточка bled/чужая. К WB (числовой слаг) и Avito/Яндекс
    Ozon-анкор НЕ применяем, чтобы не рубить легит («Office на флешке», «Office+Visio
    ключи», «Office 365 Copilot»). Позитив — по заголовку.
    """
    s = (title or "").lower()
    url_l = (url or "").lower()
    path = url_l.split("?")[0]   # без query: в трекинг-токенах Яндекса бывают «knig/guide»
    is_ozon = "ozon.ru" in url_l or "/product/" in path
    slug = path.replace("-", " ").replace("_", " ")
    blob = s + " " + slug
    if "officesuite" in s:
        return False
    if any(x in s for x in ("р7-", "р7 офис", "мойофис", "redos", "ред ос",
                            "libreoffice", "astra linux", "базальт", "rosa",
                            "кит офис", "обычный офис")):
        return False
    if _re.search(r"\bxbox\b|playstation|\bps[2345]\b|nintendo|game\s?pass|gamepass", s):
        return False
    # Не-Office продукты Microsoft (Dynamics/SharePoint/Exchange/Azure/Power BI/Defender/
    # Business Central) + Polaris — НИКОГДА не контрафактный MS Office (обычно книги/корп-ПО).
    # Visio/Project НЕ берём: бывают в легит Avito-связках «ключи Office + Visio + Project».
    if _re.search(r"dynamics|sharepoint|exchange online|exchange server|\bazure\b|"
                  r"business central|power\s?bi|\bdefender\b|polaris", blob):
        return False
    # КНИГИ — однозначные книжные маркеры (рус+транслит+англ) + автор «Фамилия И.[О.]».
    # Безопасны на всех площадках: легит MS Office не бывает «guide/учебник/энциклопедия».
    if _re.search(
        r"книг|knig|руководств|rukovodstv|учебн|uchebn|пособи|posobi|самоучит|samouchit|"
        r"справочник|spravochnik|шаг за шагом|shag za shagom|мастер[ -]?класс|master klass|"
        r"методич|монограф|monograf|лекци|для чайников|dlya chaynikov|в школе|v shkole|"
        r"быстрый старт|bystryy start|краткое|kratkoe|за 24 часа|24 chasa|24 hours|"
        r"иллюстрированн|illustrated series|свод(ные|ная)|svodn|практическ|prakticheskoe|"
        r"программировани|programmirovani|programming|implementation|implementac|migration|"
        r"раскрыти|raskryti|automated testing|коллектив|kollektiv|просто как|prosto kak|"
        r"энциклопеди|entsiklopedi|"
        r"новые горизонты|novye gorizonty|наглядно|naglyadno|мастерская|masterskaya|"
        r"самостоятельно|samostoyatelno|использование microsoft|ispolzovanie microsoft|"
        r"специальное издани|spetsialnoe izdani|patent rolls|embracing|calendar of|"
        r"bibliothek|библиотек|\blearn |\bbeginner\b|express office|office tab|passfab|складск|skladsk|"
        r"ресурсы microsoft|resursy microsoft|для тех|dlya teh|все о работе|vse o rabote|"
        r"проблемы и решени|problemy i resheni|работа на компьютере|rabota na kompyutere|"
        r"в целом|v tselom|для бухгалтер|dlya buhgalter|inside out|hands[ -]?on|introductory|"
        r"intermediate|shelly|dashboards|vba and macros|financial modeling|web components|"
        r"guide|manual|cookbook|handbook|\bbible\b|bibliya|step[\s-]?by[\s-]?step|mastering|"
        r"implementing|administrating|fundamentals|for beginners|for dummies|the complete|"
        r"\bexam\b|textbook|workbook|\(20\d\d\)",
        blob) or _re.search(r"[а-яё]{3,}\s[а-яё]\.(\s*[а-яё]\.)?", s):
        return False
    # «Никогда-не-Office» физ.мерч: Office не продаётся как календарь/кружка/духи/брелок.
    # USB/флешку СЮДА НЕ включаем — легитимный носитель («Office Pro Plus на флешке»).
    if _re.search(r"календар|kalendar|журнал|zhurnal|плакат|plakat|наклейк|nakleyk|"
                  r"кружк|kruzhk|футболк|futbolk|чехол|chehol|коврик|kovrik|очки|ochki|сумк|sumk|"
                  r"officespace|ежедневник|ezhednevnik|планинг|planing|записная книжк|zapisnaya knizh|"
                  r"туалетная вода|tualetnaya voda|парфюм|parfyum|косметик|kosmetik|консилер|konsiler|"
                  r"гирлянда|girlyanda|растяжк|rastyazhk|бейсболк|beysbolk|кепк|kepk|"
                  r"брелок|brelok|значок|znachok", blob):
        return False
    # Услуги ПК-ремонта (драйверы, лечение вирусов, восстановление, очистка) и
    # антивирусы — не продажа MS Office. «Установка Office» с ценой остаётся.
    if _re.search(r"драйвер|вирус|antivir|лечени|восстановлени|очистк[аи]|\bремонт", s):
        return False
    # OZON anti-bleed: слаг Ozon = имя товара (/product/<имя>-<id>). Если имя НЕ про Office —
    # карточка либо bled (украла чужой заголовок «Office BOX»), либо чужой товар
    # (photoshop/флешка/очки/Dynamics). У WB/Avito/Яндекс анкор не применяем.
    if is_ozon:
        m = _re.search(r"/product/(.+?)-\d{6,}", path)
        name = m.group(1) if m else ""
        if name and not ("office" in name or "ofis" in name):
            return False
        # Книжный паттерн Ozon: «… | Автор» (на Ozon легит-разделитель — буква «l», не «|»).
        if _re.search(r"\|\s*[a-zа-яё]", s):
            return False
    # Windows-only лицензии — но НЕ бандл «Windows + Office» (в нём есть Office, оставляем).
    if _re.match(r"^код windows|^ключ windows|^windows\s+\d|^лицензия windows", s) \
            and "office" not in s and "офис" not in s:
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

#!/usr/bin/env python3
"""
Парсер маркетплейсов для VDSina — заменяет старый Node.js scrape-marketplaces.js.

Запускается из cron каждые 6 часов:
  0 */6 * * * cd /opt/anti-ms-mp && .venv/bin/python parser_runner.py >> /var/log/anti-ms.log 2>&1

Также можно дёрнуть ручную сессию через n8n webhook → ssh:
  ssh root@vdsina 'cd /opt/anti-ms-mp && nohup .venv/bin/python parser_runner.py --trigger manual &'

ENV (.env):
  SUPABASE_URL=https://yqfdbuiyfkzhkhpiknob.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=<сюда service_role key>
  SCRAPER_PROXY=          (пусто = напрямую с VDSina)
  N8N_WEBHOOK_PARSER_DONE=https://.../webhook/parser-done

Записывает в Supabase:
  - listings (UPSERT по pl,product_id)
  - parser_runs (одна запись со статусом и счётчиками)
"""
import argparse
import datetime as dt
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from cloakbrowser import launch

# === Конфиг ===
# QUERIES теперь читаются из Supabase (таблица monitor_queries, active=true).
# Этот список — fallback на случай, если БД недоступна (см. load_queries()).
# Снимок проверенного рабочего набора (= активные monitor_queries на 2026-05-30).
# Статический: используется ТОЛЬКО при недоступности БД. Не авто-синхронизируется
# с дашбордом — при сильном изменении набора пере-синхронизировать вручную.
QUERIES_FALLBACK = [
    "Office ключ",          # широкий базовый (включает MS Office ключ, Office 2021/2024 ключ)
    "Office активация",     # альтернативная формулировка
    "Microsoft 365",        # подписочный сегмент (отдельный от ключей)
    "Office LTSC",          # корпоративные бессрочные
    "Office 2024",          # новейшая версия без слов «ключ/активация»
    "Office 2019",          # старая популярная версия
    "Майкрософт Офис 2021", # кириллица — отдельный сегмент (+15% новых при тесте)
    "Майкрософт Офис 2016", # кириллица — старая версия (+24% новых при тесте)
]
# Заполняется в main() из load_queries(); глобал, т.к. scrape_one_platform читает его.
QUERIES: list[str] = list(QUERIES_FALLBACK)

# Платформы и их url-фабрики
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
    # Microsoft 365 — годовые подписки
    "m365_personal":       2790,   # 1 устройство, 1 ТБ OneDrive
    "m365_family":         5290,   # до 6 пользователей, по 1 ТБ
    # Office 365 (коробочные, исторические)
    "office365_box":       6990,
    # Office 2019/2021/2024 — бессрочные лицензии (Box / ESD)
    "office_home_student": 14990,
    "office_home_bus":     22990,
    "office_pro":          39990,
    "default":             9990,
}


import re as _re


def title_ok(title: str) -> bool:
    """Фильтр контрафакта — точная копия из scraper/import_cloak_full.py.

    Карточка проходит только если описывает Microsoft Office, и не относится
    к конкурентам / книгам / Windows-only продуктам.
    """
    s = (title or "").lower()
    if "officesuite" in s:
        return False
    if any(x in s for x in ("р7-", "р7 офис", "мойофис", "redos", "ред ос",
                            "libreoffice", "astra linux", "базальт", "rosa",
                            "кит офис", "обычный офис")):
        return False
    if _re.search(r"\bкнига\b|\bруководство\b|учебник|учебн[ао]е|пособи[ея]|\bсамоучитель\b|шаг за шагом|методичк|методическ|монограф|\bлекци[ия]\b|power bi", s):
        return False
    if _re.match(r"^код windows|^ключ windows|^windows\s+\d|^лицензия windows", s):
        return False
    if _re.match(r"^office suite|^офисный пакет(?! microsoft)", s):
        return False
    # Железо с предустановленным Office (ноутбуки/моноблоки/ПК) — это устройство,
    # а не контрафактный ключ. ВАЖНО: «ключ для ноутбука/macbook/планшета» — это
    # валидный КЛЮЧ (предлог «для»), его НЕ исключаем.
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
    """Подбор офиц. цены Microsoft по типу продукта в названии (8 типов).

    Копия из scraper/import_cloak_full.py.
    """
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

# === ENV ===
def env(key, default=None, required=False):
    v = os.environ.get(key, default)
    if required and not v:
        sys.exit(f"ENV {key} обязателен")
    return v

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

load_env_file(Path(__file__).parent / ".env")
SUPABASE_URL = env("SUPABASE_URL", required=True)
SUPABASE_SERVICE_ROLE_KEY = env("SUPABASE_SERVICE_ROLE_KEY", required=True)
SCRAPER_PROXY = env("SCRAPER_PROXY", "").strip() or None
N8N_WEBHOOK = env("N8N_WEBHOOK_PARSER_DONE", "").strip() or None

# === Утилиты ===
# Время последнего лог-события = реальный прогресс скрейпа. heartbeat шлёт ЕГО,
# а не now(), чтобы watchdog ловил зависания по остановке прогресса (за ~5 мин),
# а не только по возрасту процесса (45 мин). При зависании log() не вызывается →
# heartbeat «замирает» → watchdog убивает.
_LAST_PROGRESS = {"ts": dt.datetime.now(dt.timezone.utc)}

def log(msg: str, **fields) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    _LAST_PROGRESS["ts"] = now
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{now.isoformat()}] {msg} {extras}", flush=True)

def jitter(lo=1.0, hi=2.5):
    time.sleep(random.uniform(lo, hi))

# detect_official_price удалён — используем official_price() из 8-типной логики выше

def supabase_post(path: str, payload, method="POST", prefer=None) -> dict | None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(f"{SUPABASE_URL}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        log(f"supabase {method} {path} FAIL", code=e.code, body=e.read()[:300])
        raise

def load_queries() -> list[str]:
    """Читает активные запросы из monitor_queries. При ошибке — QUERIES_FALLBACK."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/monitor_queries?select=query&active=eq.true&order=id",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        qs = [row["query"] for row in rows if row.get("query")]
        if qs:
            return qs
        log("load_queries: пустой список в БД, использую fallback")
    except Exception as e:
        log("load_queries FAIL, использую fallback", err=str(e)[:200])
    return list(QUERIES_FALLBACK)


def mark_query_parsed(query: str) -> None:
    """Обновляет last_parsed_at для запроса. Best-effort."""
    try:
        payload = json.dumps({"last_parsed_at": dt.datetime.now(dt.timezone.utc).isoformat()}).encode()
        from urllib.parse import quote as _q
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/monitor_queries?query=eq.{_q(query)}",
            data=payload, method="PATCH",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def n8n_notify(payload: dict) -> None:
    if not N8N_WEBHOOK:
        return
    try:
        req = urllib.request.Request(
            N8N_WEBHOOK,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log("n8n notify failed (продолжаю)", err=str(e)[:200])

# === Извлечение карточек ===
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
          const t = priceEl.textContent || priceEl.getAttribute('content') || '';
          const n = parseInt(t.replace(/\D/g, ''), 10);
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

BLOCK_MARKERS = ["datadome", "доступ ограничен", "captcha", "challenge",
                 "запросов с вашего ip", "возможно, что-то пошло не так"]


def detect_block(page) -> tuple[bool, str]:
    sample = page.evaluate("() => (document.body.innerText || '').slice(0, 400) + ' ' + document.title")
    lower = sample.lower()
    found = [m for m in BLOCK_MARKERS if m in lower]
    return bool(found), sample[:200]


def scroll_lazy(page, rounds=4):
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(random.uniform(0.6, 1.2))
        except Exception:
            break


def _safe_detect_block(page) -> tuple[bool, str]:
    """detect_block может бросить ExecCtx-исключение если страница в момент проверки
    делает SPA-навигацию (типично для avito antibot-redirect). Маскируем как 'blocked'."""
    try:
        return detect_block(page)
    except Exception as e:
        return True, f"ExecCtx during detect_block: {str(e)[:120]}"


def _scrape_one_page(page, platform: str, q: str, p: int, run_log, items: list[dict],
                     errors: list[dict]) -> tuple[bool, bool]:
    """Скрейпит одну страницу. Возвращает (page_ok, should_break_query).

    Любая ошибка ловится тут локально, чтобы не оборвать весь прогон платформы.
    """
    cfg = PLATFORMS[platform]
    url = cfg["url"](q, p)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        errors.append({"platform": platform, "query": q, "page": p, "message": f"goto: {str(e)[:120]}"})
        return False, True
    time.sleep(2)
    # Дожидаемся успокоения сети — анти-SPA-навигация защита.
    # Не критично, если не дождались за 8 сек — продолжаем.
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    blocked, sample = _safe_detect_block(page)
    if blocked:
        errors.append({"platform": platform, "query": q, "page": p, "message": f"BLOCKED: {sample[:120]}"})
        return False, True
    scroll_lazy(page)
    try:
        cards = page.evaluate(EXTRACT_JS[platform])
    except Exception as e:
        errors.append({"platform": platform, "query": q, "page": p, "message": f"extract: {str(e)[:120]}"})
        return False, False  # это страница упала, но другие query на этой платформе можно пробовать
    if not cards:
        return True, True
    kept = 0
    for c in cards:
        title = (c.get("title") or "").strip()
        if not title or not title_ok(title):
            continue
        c["platform"] = platform
        c["query"] = q
        items.append(c)
        kept += 1
    run_log(f"    стр.{p}: {len(cards)} → {kept} после title_ok (итого {len(items)})")
    return True, False


def scrape_one_platform(page, platform: str, run_log) -> tuple[list[dict], list[dict]]:
    """Скрейпит платформу. Любая ошибка отдельной query/page изолируется —
    остальные query пробуются, чтобы не потерять всю платформу из-за одной
    кривой страницы (типично avito SPA-navigation: "Execution context was destroyed")."""
    cfg = PLATFORMS[platform]
    items: list[dict] = []
    errors: list[dict] = []
    for q in QUERIES:
        run_log(f"  [{platform}] query='{q}'")
        for p in range(1, cfg["max_pages"] + 1):
            page_ok, should_break = _scrape_one_page(page, platform, q, p, run_log, items, errors)
            # Retry стр.1 один раз при ExecCtx ошибке — частый паттерн avito antibot
            if not page_ok and p == 1 and errors and "extract:" in (errors[-1].get("message") or ""):
                run_log(f"    стр.1 retry после extract-ошибки")
                errors.pop()  # убрать запись о первой попытке: при успешном retry не раздуваем errors→partial; при повторном сбое _scrape_one_page добавит свежую
                time.sleep(3)
                page_ok, should_break = _scrape_one_page(page, platform, q, p, run_log, items, errors)
            if should_break:
                break
            jitter()
    return items, errors


def dedup(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        k = (it["platform"], it["id"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def cleanup_old_listings(retention_days: int = 14) -> int:
    """Удалить «исчезнувшие» с маркетплейсов карточки (last_seen старше N дней).

    Запускается после каждого cron-прогона. Не блокирующая — при ошибке
    логируем и продолжаем (парсинг важнее retention).

    Возвращает количество удалённых строк (или -1 при ошибке).
    """
    cutoff = (dt.date.today() - dt.timedelta(days=retention_days)).isoformat()
    try:
        # DELETE через PostgREST: last_seen < cutoff
        # PostgREST DELETE требует фильтра — используем lt (less than)
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/listings?last_seen=lt.{cutoff}",
            method="DELETE",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Prefer": "return=representation",  # вернёт удалённые строки
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            deleted = json.loads(body) if body else []
            return len(deleted)
    except Exception as e:
        log("cleanup failed (не критично)", err=str(e)[:200])
        return -1


def cleanup_old_parser_runs(retention_days: int = 90) -> int:
    """Удалить parser_runs старше N дней. Тоже не блокирующая."""
    cutoff_dt = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).isoformat()
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/parser_runs?started_at=lt.{cutoff_dt}",
            method="DELETE",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Prefer": "return=representation",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            deleted = json.loads(body) if body else []
            return len(deleted)
    except Exception as e:
        log("cleanup parser_runs failed (не критично)", err=str(e)[:200])
        return -1


def upsert_listings(rows: list[dict]) -> int:
    if not rows:
        return 0
    today = dt.date.today().isoformat()
    payload = [
        {
            "date": today,
            "pl": r["platform"],
            "product_id": r["id"],
            "query": r["query"],
            "title": r["title"][:500],
            "url": r.get("url"),
            "price": int(r["price"]) if r.get("price") else None,
            "op": official_price(r["title"]),
            # first_seen НЕ шлём: триггер listings_first_seen_trg ставит его при
            # ПЕРВОЙ вставке (= last_seen), а при повторном upsert (merge-duplicates)
            # отсутствие поля сохраняет исходную дату. Иначе first_seen затирался на
            # сегодня каждый прогон → ломались «Свежие» и «Новых за сутки».
            "last_seen": today,
        }
        for r in rows
    ]
    BATCH = 200
    ok = 0
    for i in range(0, len(payload), BATCH):
        batch = payload[i:i + BATCH]
        supabase_post(
            "/rest/v1/listings?on_conflict=pl,product_id",
            batch,
            method="POST",
            prefer="resolution=merge-duplicates,return=minimal",
        )
        ok += len(batch)
    return ok


def insert_parser_run(trigger: str) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    resp = supabase_post(
        "/rest/v1/parser_runs",
        {
            "status": "running",
            "trigger": trigger,
            "host": socket.gethostname(),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "last_heartbeat": now,
        },
        method="POST",
        prefer="return=representation",
    )
    return resp[0]["id"] if isinstance(resp, list) else resp["id"]


def heartbeat(run_id: int) -> None:
    """Раз в 30 сек пишем last_heartbeat = время последнего ПРОГРЕССА (лог-события),
    а не now(). Так при зависании heartbeat замирает и watchdog убивает прогон за
    ~5 мин (STALE_AFTER), а не ждёт 45-мин возрастной kill."""
    ts = _LAST_PROGRESS.get("ts") or dt.datetime.now(dt.timezone.utc)
    payload = json.dumps({"last_heartbeat": ts.isoformat()}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/parser_runs?id=eq.{run_id}",
        data=payload,
        method="PATCH",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass  # heartbeat best-effort; watchdog убьёт всё равно если важно


def start_heartbeat_thread(run_id: int, interval: int = 30) -> threading.Event:
    """Запускает фоновый поток, который каждые `interval` секунд пишет heartbeat.
    Возвращает Event, который надо `.set()` чтобы остановить поток."""
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            heartbeat(run_id)

    t = threading.Thread(target=loop, daemon=True, name=f"heartbeat-{run_id}")
    t.start()
    return stop


def finalize_parser_run(run_id: int, totals: dict, errors: list[dict], status: str) -> None:
    payload = {
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": status,
        "totals": totals,
        "errors": errors,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/parser_runs?id=eq.{run_id}",
        data=data,
        method="PATCH",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=20).read()


def upsert_daily_stats(rows: list[dict]) -> None:
    """Дневной снимок статистики в daily_stats (upsert по day,pl). Решает проблему
    схлопывания listings.last_seen — история динамики живёт здесь, не в листингах."""
    if not rows:
        return
    data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/daily_stats?on_conflict=day,pl",
        data=data,
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log("daily_stats upsert failed (не критично)", err=str(e)[:200])


def platform_daily_stat(plat: str, items_d: list[dict]) -> dict:
    """Считает дневной срез по платформе: всего, контрафакт, средний дисконт."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cf = []
    for c in items_d:
        price = c.get("price") or 0
        op = official_price(c.get("title") or "")
        if price > 0 and op > 0 and price < 0.5 * op:
            cf.append(round((1 - price / op) * 100))
    avg_disc = round(sum(cf) / len(cf)) if cf else None
    return {"day": today, "pl": plat, "total": len(items_d),
            "counterfeit": len(cf), "avg_disc": avg_disc,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platforms", default=",".join(PLATFORMS), help="ozon,wildberries,yandex,avito")
    ap.add_argument("--trigger", default="cron", choices=["cron", "manual", "watcher", "dashboard"])
    ap.add_argument("--only-query", default=None,
                    help="Парсить только этот запрос (для быстрого прогона нового запроса с дашборда)")
    ap.add_argument("--no-notify", action="store_true",
                    help="Не слать n8n/Telegram-уведомление (для ночного cron: один отчёт шлёт оркестратор в 04:00)")
    args = ap.parse_args()
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    # Порядок: сначала надёжные (ozon, wildberries), потом склонные к зависанию
    # (avito SPA-redirect, yandex DataDome) — yandex ПОСЛЕДНИМ. Так зависание одной
    # площадки не лишает данных остальные (каждая апсертится сразу после скрейпа).
    SCRAPE_ORDER = {"ozon": 0, "wildberries": 1, "avito": 2, "yandex": 3}
    platforms.sort(key=lambda p: SCRAPE_ORDER.get(p, 9))

    # QUERIES: либо один запрос (--only-query), либо активные из monitor_queries.
    global QUERIES
    if args.only_query:
        QUERIES = [args.only_query]
        log("режим single-query", query=args.only_query)
    else:
        QUERIES = load_queries()
    log("queries загружены", count=len(QUERIES))

    log("=== run start ===", platforms=",".join(platforms), proxy=SCRAPER_PROXY or "direct")
    run_id = insert_parser_run(args.trigger)
    log("run id", id=run_id, pid=os.getpid())

    # Heartbeat-поток: каждые 30 сек обновляет parser_runs.last_heartbeat.
    # Watchdog на VDSina убьёт прогон, если heartbeat молчит >5 мин.
    heartbeat_stop = start_heartbeat_thread(run_id, interval=30)

    totals: dict = {}
    all_errors: list[dict] = []
    daily_rows: list[dict] = []

    launch_kwargs = {"headless": True, "humanize": True}
    if SCRAPER_PROXY:
        launch_kwargs["proxy"] = SCRAPER_PROXY
    browser = launch(**launch_kwargs)
    try:
        page = browser.new_page()
        # Защита от вечного зависания: жёсткие таймауты на всех уровнях.
        # Cloakbrowser/Playwright по умолчанию ждёт 30 сек, но page.evaluate
        # вообще без timeout — поэтому страница с DataDome-challenge может
        # висеть бесконечно. Эти setter'ы делают timeout явным.
        try:
            page.set_default_navigation_timeout(45000)
            page.set_default_timeout(30000)
        except Exception:
            pass
        for plat in platforms:
            try:
                items, errors = scrape_one_platform(page, plat, log)
                items_d = dedup(items)
                ok = upsert_listings(items_d)
                totals[plat] = {"found": len(items), "unique": len(items_d), "upserted": ok}
                ds = platform_daily_stat(plat, items_d)
                daily_rows.append(ds)
                # daily_stats пишем СРАЗУ по площадке (а не в конце прогона) — иначе
                # зависание на последней площадке теряет дневной срез успевших.
                if not args.only_query:
                    try:
                        upsert_daily_stats([ds])
                    except Exception as e:
                        log("daily_stats (инкр.) fail", err=str(e)[:150])
                all_errors.extend(errors)
                log(f"  [{plat}] done", **totals[plat])
            except Exception as e:
                all_errors.append({"platform": plat, "stage": "scrape_platform", "message": str(e)[:300]})
                log(f"  [{plat}] FAIL", err=str(e)[:200])
    finally:
        browser.close()

    has_data = sum(t.get("upserted", 0) for t in totals.values()) > 0
    status = "ok" if has_data and not all_errors else ("partial" if has_data else "failed")

    # Retention: только если прогон успешен (ok/partial с данными).
    # Если прогон полностью провалился — не удаляем, иначе можем потерять
    # данные при сетевой проблеме (хочется иметь хоть что-то на дашборде).
    if has_data:
        deleted = cleanup_old_listings(retention_days=14)
        log("retention: удалено карточек старше 14 дней", deleted=deleted)
        totals["_retention"] = {"listings_deleted": deleted}
        runs_deleted = cleanup_old_parser_runs(retention_days=90)
        if runs_deleted > 0:
            log("retention: удалено parser_runs старше 90 дней", deleted=runs_deleted)

    # daily_stats теперь пишется инкрементально внутри цикла (по каждой площадке
    # сразу) — чтобы зависание на последней площадке не теряло дневной срез
    # успевших. Здесь только логируем итог.
    if has_data and not args.only_query:
        log("daily_stats записан инкрементально", platforms=len(daily_rows))

    # Отметим запросы как обработанные (для дашборда — когда последний раз парсился)
    if has_data:
        for q in QUERIES:
            mark_query_parsed(q)

    heartbeat_stop.set()  # стоп heartbeat-потока перед финализацией
    finalize_parser_run(run_id, totals, all_errors, status)
    log("=== run end ===", status=status, totals=totals, errors=len(all_errors))
    if not args.no_notify:
        n8n_notify({"run_id": run_id, "status": status, "totals": totals, "errors_count": len(all_errors)})
    else:
        log("n8n notify пропущен (--no-notify): отчёт пришлёт оркестратор")
    # ВАЖНО: os._exit, а не return/sys.exit. cloakbrowser/Playwright оставляет
    # non-daemon потоки и дочерний chromium, из-за которых процесс зависал после
    # завершения работы (run end в логе есть, а процесс жил ещё часами и держал
    # cloakbrowser-ресурс — watchdog не ловил, т.к. run уже finalized=ok).
    # os._exit рвёт процесс немедленно, минуя ожидание потоков и atexit.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0 if status != "failed" else 1)


if __name__ == "__main__":
    sys.exit(main())

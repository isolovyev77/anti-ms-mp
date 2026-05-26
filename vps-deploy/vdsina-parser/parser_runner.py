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
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from cloakbrowser import launch

# === Конфиг ===
QUERIES = [
    "Microsoft Office ключ активации",
    "Microsoft Office 365 ключ",
    "Office 2021 ключ активации",
    "Office 2024 ключ активации",
    "MS Office ключ активации",
]

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
    "office365": 6990,
    "office2021home": 14990,
    "office2021hb": 22990,
    "office_default": 9990,
}

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
def log(msg: str, **fields) -> None:
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{ts}] {msg} {extras}", flush=True)

def jitter(lo=1.0, hi=2.5):
    time.sleep(random.uniform(lo, hi))

def detect_official_price(title: str) -> int:
    t = (title or "").lower()
    if "365" in t or "personal" in t or "подписк" in t:
        return OFFICIAL_PRICES["office365"]
    if "home and business" in t or " hb" in t:
        return OFFICIAL_PRICES["office2021hb"]
    if "2021" in t and "home" in t:
        return OFFICIAL_PRICES["office2021home"]
    return OFFICIAL_PRICES["office_default"]

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
        const priceText = card.querySelector('.price__lower-price')?.textContent || card.querySelector('ins')?.textContent || '';
        const price = parseInt(priceText.replace(/\D/g, ''), 10) || 0;
        out.push({ id: String(id), title, price, url: href.startsWith('http') ? href : 'https://www.wildberries.ru' + href });
      });
      return out;
    }""",
    "yandex": r"""
    () => {
      const out = [];
      document.querySelectorAll('article[data-auto="searchOrganic"]').forEach(card => {
        const link = card.querySelector('a[data-auto="snippet-link"]') || card.querySelector('a[href*="/card/"]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        const m = href.match(/\/card\/[^\/]+\/(\d+)/);
        if (!m) return;
        const title = (card.querySelector('[data-auto="snippet-title"]')?.textContent || link.textContent || '').trim();
        const priceText = card.querySelector('[data-auto="snippet-price"]')?.textContent || '';
        const price = parseInt(priceText.replace(/\D/g, ''), 10) || 0;
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


def scrape_one_platform(page, platform: str, run_log) -> tuple[list[dict], list[dict]]:
    cfg = PLATFORMS[platform]
    items: list[dict] = []
    errors: list[dict] = []
    for q in QUERIES:
        run_log(f"  [{platform}] query='{q}'")
        for p in range(1, cfg["max_pages"] + 1):
            url = cfg["url"](q, p)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                errors.append({"platform": platform, "query": q, "page": p, "message": str(e)[:200]})
                break
            time.sleep(2)
            blocked, sample = detect_block(page)
            if blocked:
                errors.append({"platform": platform, "query": q, "page": p, "message": f"BLOCKED: {sample[:120]}"})
                break
            scroll_lazy(page)
            try:
                cards = page.evaluate(EXTRACT_JS[platform])
            except Exception as e:
                errors.append({"platform": platform, "query": q, "page": p, "message": f"extract: {str(e)[:120]}"})
                cards = []
            if not cards:
                break
            for c in cards:
                c["platform"] = platform
                c["query"] = q
                items.append(c)
            run_log(f"    стр.{p}: +{len(cards)} (итого {len(items)})")
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
            "op": detect_official_price(r["title"]),
            "first_seen": today,
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
    resp = supabase_post(
        "/rest/v1/parser_runs",
        {"status": "running", "trigger": trigger, "host": socket.gethostname()},
        method="POST",
        prefer="return=representation",
    )
    return resp[0]["id"] if isinstance(resp, list) else resp["id"]


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platforms", default=",".join(PLATFORMS), help="ozon,wildberries,yandex,avito")
    ap.add_argument("--trigger", default="cron", choices=["cron", "manual", "watcher"])
    args = ap.parse_args()
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]

    log("=== run start ===", platforms=",".join(platforms), proxy=SCRAPER_PROXY or "direct")
    run_id = insert_parser_run(args.trigger)
    log("run id", id=run_id)

    totals: dict = {}
    all_errors: list[dict] = []

    launch_kwargs = {"headless": True, "humanize": True}
    if SCRAPER_PROXY:
        launch_kwargs["proxy"] = SCRAPER_PROXY
    browser = launch(**launch_kwargs)
    try:
        page = browser.new_page()
        for plat in platforms:
            try:
                items, errors = scrape_one_platform(page, plat, log)
                items_d = dedup(items)
                ok = upsert_listings(items_d)
                totals[plat] = {"found": len(items), "unique": len(items_d), "upserted": ok}
                all_errors.extend(errors)
                log(f"  [{plat}] done", **totals[plat])
            except Exception as e:
                all_errors.append({"platform": plat, "stage": "scrape_platform", "message": str(e)[:300]})
                log(f"  [{plat}] FAIL", err=str(e)[:200])
    finally:
        browser.close()

    has_data = sum(t.get("upserted", 0) for t in totals.values()) > 0
    status = "ok" if has_data and not all_errors else ("partial" if has_data else "failed")
    finalize_parser_run(run_id, totals, all_errors, status)
    log("=== run end ===", status=status, totals=totals, errors=len(all_errors))
    n8n_notify({"run_id": run_id, "status": status, "totals": totals, "errors_count": len(all_errors)})
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Ступень каскада: повторный проход CloakBrowser через РФ-ПРОКСИ (свежий IP, напр.
RuVDS-туннель socks5://127.0.0.1:1081). Вызывается боевым парсером ОТДЕЛЬНЫМ ПРОЦЕССОМ,
когда CloakBrowser упёрся в антибот на прямом IP VDSina — другой РФ-IP с низким velocity
часто проходит там, где прямой флагнут. Subprocess → RAM освобождается по завершении.

Тот же venv, что у основного парсера (cloakbrowser), — НЕ camoufox-venv.
Запуск: <cloak-venv>/python cloak_proxy_scrape.py <platform> <q1> [q2 ...] --proxy socks5://127.0.0.1:1081 [--max-pages N]
Вывод: одна строка JSON в stdout — {"platform","engine":"cloak-proxy","blocked":bool,"items":[...],"stats":{...}}
Диагностика — в stderr (не мешает JSON). extractors.py должен лежать рядом.
"""
import sys
import json
import time
import argparse

from extractors import PLATFORMS, EXTRACT_JS, BLOCK_MARKERS, title_ok
from cloakbrowser import launch


def err(msg):
    print(f"[cloak-proxy] {msg}", file=sys.stderr, flush=True)


def _blocked(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in BLOCK_MARKERS)


def scrape_platform(platform: str, queries: list, max_pages: int, proxy: str):
    cfg = PLATFORMS[platform]
    extract = EXTRACT_JS[platform]
    pages_max = min(max_pages, cfg.get("max_pages", 5))
    items, errors = [], []
    blocked_any = False

    kw = {"headless": True, "humanize": True}
    if proxy:
        kw["proxy"] = {"server": proxy}
        err(f"через прокси {proxy}")
    browser = launch(**kw)
    try:
        page = browser.new_page()
        try:
            page.set_default_navigation_timeout(60000)
            page.set_default_timeout(40000)
        except Exception:
            pass
        for q in queries:
            for p in range(1, pages_max + 1):
                url = cfg["url"](q, p)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    errors.append({"q": q, "p": p, "err": f"goto: {str(e)[:120]}"})
                    break
                time.sleep(2)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                try:
                    sample = page.evaluate(
                        "() => (document.body.innerText || '').slice(0,400) + ' ' + document.title")
                except Exception as e:
                    sample = f"ExecCtx: {str(e)[:100]}"
                if _blocked(sample):
                    blocked_any = True
                    errors.append({"q": q, "p": p, "err": f"BLOCKED: {sample[:100]}"})
                    err(f"{platform} q='{q}' p={p}: BLOCKED")
                    break
                for _ in range(4):
                    try:
                        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                        time.sleep(0.8)
                    except Exception:
                        break
                try:
                    cards = page.evaluate(extract)
                except Exception as e:
                    errors.append({"q": q, "p": p, "err": f"extract: {str(e)[:120]}"})
                    continue
                if not cards:
                    break
                kept = 0
                for c in cards:
                    t = (c.get("title") or "").strip()
                    if not t or not title_ok(t):
                        continue
                    c["platform"] = platform
                    c["query"] = q
                    items.append(c)
                    kept += 1
                err(f"{platform} q='{q}' p={p}: {len(cards)} карточек → {kept} после title_ok")
    finally:
        browser.close()
    return items, errors, blocked_any


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("platform")
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()

    if args.platform not in PLATFORMS:
        print(json.dumps({"error": f"unknown platform: {args.platform}"}))
        return 2

    t0 = time.time()
    try:
        items, errors, blocked = scrape_platform(args.platform, args.queries, args.max_pages, args.proxy)
    except Exception as e:
        print(json.dumps({"platform": args.platform, "engine": "cloak-proxy",
                          "error": repr(e)[:300], "items": [], "blocked": False}))
        return 1

    # дедуп по (platform, id) — как dedup() в парсере
    seen, uniq = set(), []
    for it in items:
        k = (it["platform"], it.get("id"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)

    out = {
        "platform": args.platform,
        "engine": "cloak-proxy",
        "blocked": blocked,
        "items": uniq,
        "stats": {
            "found": len(items),
            "unique": len(uniq),
            "with_price": sum(1 for i in uniq if i.get("price")),
            "errors": len(errors),
            "elapsed_s": round(time.time() - t0, 1),
        },
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

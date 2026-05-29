#!/usr/bin/env python3
"""Диагностика DOM цены/заголовка для любой площадки.

Использует PLATFORMS + EXTRACT_JS из самого parser_runner, поэтому всегда
показывает РОВНО то, что сейчас вытаскивает парсер, и рядом — где реально
лежит цена (листовые элементы с ₽). Нужен оркестратору для авто-починки:
сравнив «что взял экстрактор» с «где цена на самом деле», видно, какой
селектор умер после смены вёрстки маркетплейса.

Запуск:  .venv/bin/python diag_price.py <platform> "<query>"
Пример:  .venv/bin/python diag_price.py yandex "Microsoft Office 2021 ключ"
"""
import os, sys, time, json
from pathlib import Path

sys.path.insert(0, "/opt/anti-ms-mp")
from parser_runner import PLATFORMS, EXTRACT_JS, SCRAPER_PROXY  # noqa
from cloakbrowser import launch


def main():
    if len(sys.argv) < 3:
        print("usage: diag_price.py <platform> <query>"); return 2
    platform, query = sys.argv[1], sys.argv[2]
    if platform not in PLATFORMS:
        print(f"unknown platform {platform}, есть: {list(PLATFORMS)}"); return 2

    url = PLATFORMS[platform]["url"](query, 1)
    kw = {"headless": True, "humanize": True}
    if SCRAPER_PROXY:
        kw["proxy"] = SCRAPER_PROXY
    b = launch(**kw)
    try:
        pg = b.new_page()
        pg.set_default_navigation_timeout(45000); pg.set_default_timeout(30000)
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(2)
        try: pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception: pass
        for _ in range(4):
            pg.evaluate("window.scrollBy(0, document.body.scrollHeight)"); time.sleep(1)

        # 1) Что СЕЙЧАС берёт боевой экстрактор парсера
        try:
            current = pg.evaluate(EXTRACT_JS[platform])[:8]
        except Exception as e:
            current = [{"error": str(e)[:200]}]

        # 2) Где реально лежит цена/заголовок в DOM (первые карточки)
        # Селектор карточки берём эвристически по площадке.
        card_sel = {
            "ozon": '[class*="tile"], a[href*="/product/"]',
            "wildberries": 'article.product-card, .product-card',
            "yandex": 'article[data-auto="searchOrganic"]',
            "avito": '[data-marker="item"]',
        }.get(platform, "*")
        dom = pg.evaluate(r"""(sel) => {
          const norm = s => (s||'').replace(/[  ]/g,' ');
          const cards = [...document.querySelectorAll(sel)].slice(0,6);
          return cards.map(card => {
            const rub = [];
            card.querySelectorAll('*').forEach(el => {
              if (el.children.length > 1) return;
              const t = norm(el.textContent).trim();
              if (/\d\s*₽/.test(t) && t.length < 30)
                rub.push({tag: el.tagName, da: el.getAttribute('data-auto')||'',
                          cl: (el.className||'').toString().slice(0,32), txt: t.slice(0,24)});
            });
            const titleEl = card.querySelector('[data-auto="snippet-title"], [class*="title"], h3, h4, [class*="name"]');
            return {title: (titleEl?.textContent||'').slice(0,50), rub: rub.slice(0,5)};
          });
        }""", card_sel)

        print(json.dumps({
            "platform": platform, "query": query, "url": url,
            "current_extractor_output": current,
            "dom_price_elements": dom,
        }, ensure_ascii=False, indent=1))
    finally:
        b.close()
    sys.stdout.flush(); os._exit(0)


if __name__ == "__main__":
    sys.exit(main())

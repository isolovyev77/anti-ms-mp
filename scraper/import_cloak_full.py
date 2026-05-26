#!/usr/bin/env python3
"""Импорт полного парсинга через CloakBrowser+VDSina (соседняя сессия 26.05).

Источники в `cloakbrowser-lab/` (директория в .gitignore):
  - ozon_cloak_full.json   — 1823 сырых → ~? после titleOk
  - avito_cloak_full.json  — 1030 сырых → ~? после titleOk
  - wb_cloak_full.json     — 426 сырых
  - ym_cloak_full.json     — 266 сырых

Полная замена прежних снапшотов всех 4 площадок (старые 19.03 переходят
в архив). Записывает 2 файла:
  - scraper/mon_data.json         — WB + ЯМ (формат: ключ 'data')
  - scraper/mon_data_captcha.json — Ozon + Avito

Дальше merge_mon_raw.py подмешивает в дашборд.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / 'cloakbrowser-lab'

SOURCES = {
    'ozon':        LAB / 'ozon_cloak_full.json',
    'avito':       LAB / 'avito_cloak_full.json',
    'wildberries': LAB / 'wb_cloak_full.json',
    'yandex':      LAB / 'ym_cloak_full.json',
}

OFFICIAL_PRICES = {
    'office365':         6990,
    'office2021home':   14990,
    'office2021homebus': 22990,
    'default':           9990,
}


def title_ok(title: str) -> bool:
    s = title.lower()
    # Исключаем конкурентов и не-Microsoft
    if 'officesuite' in s:
        return False
    if any(x in s for x in ('р7-', 'р7 офис', 'мойофис', 'redos', 'ред ос',
                            'libreoffice', 'astra linux', 'базальт', 'rosa',
                            'кит офис', 'обычный офис')):
        return False
    # Книги, руководства, обучение
    if re.search(r'\bкнига\b|\bруководство\b|\bучебник\b|\bсамоучитель\b|шаг за шагом', s):
        return False
    # Только Windows без Office
    if re.match(r'^код windows|^ключ windows|^windows\s+\d|^лицензия windows', s):
        return False
    if re.match(r'^office suite|^офисный пакет(?! microsoft)', s):
        return False
    # Положительные критерии: должны быть Microsoft Office / Office 365 / Office 2021 etc.
    return (
        'office' in s
        or ('365' in s and ('microsoft' in s or 'ms' in s.split()))
        or ('офис' in s and 'microsoft' in s)
    )


def official_price(title: str) -> int:
    t = title.lower()
    if '365' in t or 'personal' in t:
        return OFFICIAL_PRICES['office365']
    if 'home and business' in t or 'home & business' in t:
        return OFFICIAL_PRICES['office2021homebus']
    if any(y in t for y in ('2021', '2024', '2019', '2016', 'professional', 'pro plus', 'pro 365')):
        return OFFICIAL_PRICES['office2021home']
    return OFFICIAL_PRICES['default']


PL_PREFIX = {'ozon': 'OZ', 'avito': 'AV', 'wildberries': 'WB', 'yandex': 'YM'}


def normalize_item(pl: str, it: dict, today: str) -> dict | None:
    raw_id = str(it.get('id') or '')
    if not raw_id:
        return None
    title = (it.get('title') or '').strip()
    price = int(it.get('price') or 0)
    if not title or not price:
        return None
    if not title_ok(title):
        return None
    if price < 10 or price > 100000:
        return None
    url = it.get('url') or ''
    return {
        'date': today,
        'pl': pl,
        'id': f"{PL_PREFIX[pl]}-{raw_id}",
        'query': it.get('query', ''),
        'title': title[:120],
        'url': url,
        'price': price,
        'op': official_price(title),
        'regDays': None,
        'auth': None,
    }


def main() -> int:
    today = date.today().isoformat()
    by_pl: dict[str, list[dict]] = {}
    stats_raw: dict[str, int] = {}

    for pl, src in SOURCES.items():
        if not src.exists():
            print(f'SKIP {pl}: нет файла {src}')
            by_pl[pl] = []
            continue
        data = json.loads(src.read_text(encoding='utf-8'))
        raw_items = data.get('items', [])
        stats_raw[pl] = len(raw_items)
        out = []
        seen = set()
        for it in raw_items:
            n = normalize_item(pl, it, today)
            if not n:
                continue
            if n['id'] in seen:
                continue
            seen.add(n['id'])
            out.append(n)
        by_pl[pl] = out
        print(f"{pl:12s} {stats_raw[pl]:5d} raw → {len(out):5d} после titleOk+dedup")

    # Записываем 2 файла: WB+ЯМ → mon_data.json, Ozon+Avito → mon_data_captcha.json
    main_items = by_pl['wildberries'] + by_pl['yandex']
    captcha_items = by_pl['ozon'] + by_pl['avito']

    (ROOT / 'scraper' / 'mon_data.json').write_text(json.dumps({
        'generated': f'{today}T00:00:00',
        'source': 'cloakbrowser+vdsina',
        'total': len(main_items),
        'queries': sorted({x['query'] for x in main_items if x['query']}),
        'data': main_items,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    (ROOT / 'scraper' / 'mon_data_captcha.json').write_text(json.dumps({
        'generated': f'{today}T00:00:00',
        'source': 'cloakbrowser+vdsina',
        'total': len(captcha_items),
        'queries': sorted({x['query'] for x in captcha_items if x['query']}),
        'data': captcha_items,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    total = len(main_items) + len(captcha_items)
    print(f"\nЗаписано: mon_data.json ({len(main_items)}) + mon_data_captcha.json ({len(captcha_items)}) = {total}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

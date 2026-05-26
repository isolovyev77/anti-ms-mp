#!/usr/bin/env python3
"""Конвертировать ozon_scrape_YYYY-MM-DD.json (выгруженный из Chrome через
Claude in Chrome / AmneziaVPN split tunneling) в формат mon_data_captcha.json,
который merge_mon_raw.py ждёт для Ozon+Avito.

Применяется потому что Playwright headless + VDSina datacenter IP не пускает
Ozon, а обычный Chrome пользователя с AmneziaVPN пускает. Скрипт парсит
Ozon JSON-API через расширение, записывает в Downloads, а этот импортёр
доводит до формата дашборда.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'scraper' / 'ozon_scrape_2026-05-26.json'
DST = ROOT / 'scraper' / 'mon_data_captcha.json'

OFFICIAL_PRICES = {
    'office365': 6990,
    'office2021home': 14990,
    'office2021homebus': 22990,
    'default': 9990,
}


def official_price(title: str) -> int:
    t = title.lower()
    if '365' in t or 'personal' in t:
        return OFFICIAL_PRICES['office365']
    if 'home and business' in t or 'home & business' in t or ' hb ' in f' {t} ':
        return OFFICIAL_PRICES['office2021homebus']
    if any(y in t for y in ('2021', '2024', '2019', '2016', 'professional', 'pro plus')):
        return OFFICIAL_PRICES['office2021home']
    return OFFICIAL_PRICES['default']


def main() -> int:
    raw = json.loads(SRC.read_text(encoding='utf-8'))
    items = raw.get('items', [])
    today = date.today().isoformat()

    out_items = []
    for it in items:
        price = int(it['price'])
        title = it['title'].strip()
        if not title:
            continue
        out_items.append({
            'date': today,
            'pl': 'ozon',
            'id': f"OZ-{it['id']}",
            'query': it.get('query', ''),
            'title': title[:120],
            'url': it['url'],
            'price': price,
            'op': official_price(title),
            'regDays': None,
            'auth': None,
        })

    payload = {
        'generated': raw.get('generated'),
        'source': raw.get('source', 'ozon-json-api-via-amneziavpn'),
        'total': len(out_items),
        'queries': sorted({x['query'] for x in out_items if x['query']}),
        'data': out_items,
    }
    DST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Записано {len(out_items)} карточек в {DST}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

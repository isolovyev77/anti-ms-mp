#!/usr/bin/env python3
"""Импорт свежих Avito-карточек из CloakBrowser (cloakbrowser-lab/avito_cloak_vdsina.json)
в формат, который merge_mon_raw.py подмешивает в дашборд.

CloakBrowser обходит antibot Avito через подделку TLS/JA3-fingerprint и
подмену User-Agent на нативный мобильный/десктопный профиль. Запускается с
SOCKS5-туннелем VDSina → российский datacenter IP. Antibot Avito пропускает.

Свежие карточки добавляются ПОВЕРХ существующего снапшота (append-режим в
merge_mon_raw.py для avito), а не заменяют его — старые 737 от 19.03 остаются
как исторический срез, свежие 30 показывают актуальную ситуацию на дату парсинга.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'cloakbrowser-lab' / 'avito_cloak_vdsina.json'
DST = ROOT / 'scraper' / 'mon_data_captcha.json'
DASHBOARD = ROOT / 'anti-ms-dashboard' / 'index_old.html'

# Старые avito-карточки из дашборда (от 19.03) подтягиваем сюда, чтобы merge_mon_raw.py
# не стёр их при перезаписи MON_RAW. Свежие 30 добавляются ПОВЕРХ — суммарный набор
# больше, чем чисто исторический.
RAW_OBJ_RE = re.compile(
    r"\{date:'(?P<date>[^']*)',pl:'(?P<pl>[^']*)',id:'(?P<id>[^']*)',"
    r"query:'(?P<query>(?:[^'\\]|\\.)*)',title:'(?P<title>(?:[^'\\]|\\.)*)',"
    r"url:'(?P<url>[^']*)',price:(?P<price>\d+),op:(?P<op>\d+),"
    r"regDays:(?P<regDays>null|\d+),auth:(?P<auth>null|true|false)\}"
)


def parse_js_string(raw: str) -> str:
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"'": "'", '"': '"', '\\': '\\', 'n': '\n', 't': '\t'}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def load_old_avito_from_dashboard() -> list[dict]:
    html = DASHBOARD.read_text(encoding='utf-8')
    start = html.find('const MON_RAW = [')
    end = html.find('\n    ];', start)
    block = html[start:end]
    out = []
    for m in RAW_OBJ_RE.finditer(block):
        if m.group('pl') != 'avito':
            continue
        out.append({
            'date': m.group('date'),
            'pl': 'avito',
            'id': m.group('id'),
            'query': parse_js_string(m.group('query')),
            'title': parse_js_string(m.group('title')),
            'url': m.group('url'),
            'price': int(m.group('price')),
            'op': int(m.group('op')),
            'regDays': None if m.group('regDays') == 'null' else int(m.group('regDays')),
            'auth': None if m.group('auth') == 'null' else (m.group('auth') == 'true'),
        })
    return out

OFFICIAL_PRICES = {
    'office365': 6990,
    'office2021home': 14990,
    'office2021homebus': 22990,
    'default': 9990,
}


def title_ok(t: str) -> bool:
    s = t.lower()
    if 'officesuite' in s:
        return False
    if any(x in s for x in ('р7-', 'р7 ', 'мойофис', 'redos', 'ред ос', 'libreoffice')):
        return False
    if re.match(r'^код windows|^ключ windows|^windows\s+\d|^лицензия windows', s):
        return False
    return 'office' in s or ('365' in s and 'microsoft' in s) or ('офис' in s and 'microsoft' in s)


def official_price(title: str) -> int:
    t = title.lower()
    if '365' in t or 'personal' in t:
        return OFFICIAL_PRICES['office365']
    if 'home and business' in t or 'home & business' in t:
        return OFFICIAL_PRICES['office2021homebus']
    if any(y in t for y in ('2021', '2024', '2019', '2016', 'professional', 'pro plus')):
        return OFFICIAL_PRICES['office2021home']
    return OFFICIAL_PRICES['default']


def main() -> int:
    raw = json.loads(SRC.read_text(encoding='utf-8'))
    items = raw.get('items', [])
    today = date.today().isoformat()

    # Сохраняем существующий ozon-блок mon_data_captcha.json (от Chrome+AmneziaVPN)
    existing = []
    if DST.exists():
        try:
            existing = json.loads(DST.read_text(encoding='utf-8')).get('data', [])
            existing = [x for x in existing if x.get('pl') != 'avito']  # avito пересоберём
            print(f'Сохранено существующих не-avito записей: {len(existing)}')
        except Exception as e:
            print(f'WARN: не смог прочитать существующий {DST}: {e}')

    # Подтягиваем старые avito из дашборда (от 19.03), чтобы merge их не стёр
    old_avito = load_old_avito_from_dashboard()
    print(f'Старых avito в дашборде: {len(old_avito)}')

    out_items = list(existing) + old_avito  # начинаем со всех старых
    seen_ids = {(x['pl'], x['id']) for x in out_items}

    added_fresh = 0
    skipped_filter = 0
    for it in items:
        title = (it.get('title') or '').strip()
        price = int(it.get('price') or 0)
        if not title or not price:
            continue
        if not title_ok(title):
            skipped_filter += 1
            continue
        cid = f"AV-{it['id']}"
        if ('avito', cid) in seen_ids:
            # уже есть в старом снапшоте — обновим запись свежей (дата/цена могли измениться)
            out_items = [x for x in out_items if not (x['pl'] == 'avito' and x['id'] == cid)]
        seen_ids.add(('avito', cid))
        out_items.append({
            'date': today,
            'pl': 'avito',
            'id': cid,
            'query': it.get('query', ''),
            'title': title[:120],
            'url': it.get('url', f"https://www.avito.ru/items/{it['id']}"),
            'price': price,
            'op': official_price(title),
            'regDays': None,
            'auth': None,
        })
        added_fresh += 1

    payload = {
        'generated': raw.get('generated'),
        'source': 'cloakbrowser-vdsina + chrome-amneziavpn',
        'total': len(out_items),
        'data': out_items,
    }
    DST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    avito_count = sum(1 for x in out_items if x['pl'] == 'avito')
    ozon_count = sum(1 for x in out_items if x['pl'] == 'ozon')
    print(f'Avito: всего {avito_count} (старых {len(old_avito)} + свежих {added_fresh}, отброшено по titleOk: {skipped_filter})')
    print(f'Ozon: {ozon_count}')
    print(f'Записано {len(out_items)} карточек в {DST}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

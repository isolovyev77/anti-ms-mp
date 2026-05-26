#!/usr/bin/env python3
"""Слить свежие WB+YM (из mon_data.json) с уже имеющимися Ozon+Avito
из anti-ms-dashboard/index.html и собрать новый MON_RAW.

Ozon и Avito блокируют datacenter IP VDSina, парсятся только с residential —
поэтому для этих площадок сохраняем последний валидный снапшот мониторинга.

Выход: новый блок `const MON_RAW = [...]` подменяется внутри index.html.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / 'anti-ms-dashboard' / 'index.html'
MON_DATA = ROOT / 'scraper' / 'mon_data.json'
MON_DATA_CAPTCHA = ROOT / 'scraper' / 'mon_data_captcha.json'

# Какие площадки в каждом источнике считать «свежими» (заменяющими старый снапшот).
# WB+ЯМ парсятся через VDSina-прокси (mon_data.json).
# Ozon+Avito парсятся headful через scrape-with-captcha.js (mon_data_captcha.json).
FRESH_PLATFORMS = {
    MON_DATA: {'wildberries', 'yandex'},
    MON_DATA_CAPTCHA: {'ozon', 'avito'},
}

# Парсим объекты MON_RAW из index.html через regex - формат стабильный JS-литерал.
# Каждая запись: {date:'...',pl:'...',id:'...',query:'...',title:'...',url:'...',price:N,op:N,regDays:null,auth:null}
RAW_OBJ_RE = re.compile(
    r"\{date:'(?P<date>[^']*)',"
    r"pl:'(?P<pl>[^']*)',"
    r"id:'(?P<id>[^']*)',"
    r"query:'(?P<query>(?:[^'\\]|\\.)*)',"
    r"title:'(?P<title>(?:[^'\\]|\\.)*)',"
    r"url:'(?P<url>[^']*)',"
    r"price:(?P<price>\d+),"
    r"op:(?P<op>\d+),"
    r"regDays:(?P<regDays>null|\d+),"
    r"auth:(?P<auth>null|true|false)\}"
)


def js_string_escape(value: str) -> str:
    """Минимальный escaping для JS-литерала в одинарных кавычках."""
    return (
        value
        .replace('\\', '\\\\')
        .replace("'", "\\'")
        .replace('\n', ' ')
        .replace('\r', ' ')
    )


def parse_js_string(raw: str) -> str:
    """Разэскейпить JS-строку из одинарных кавычек обратно в Python."""
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in ("'", '"', '\\'):
                out.append(nxt)
                i += 2
                continue
            if nxt == 'n':
                out.append('\n')
                i += 2
                continue
            if nxt == 't':
                out.append('\t')
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def load_old_mon_raw(html: str) -> list[dict]:
    """Извлечь массив старых записей MON_RAW из index.html."""
    start = html.find('const MON_RAW = [')
    end = html.find('\n    ];', start)
    if start < 0 or end < 0:
        raise RuntimeError('Не нашёл MON_RAW в index.html')
    block = html[start:end]
    items = []
    for m in RAW_OBJ_RE.finditer(block):
        items.append({
            'date': m.group('date'),
            'pl': m.group('pl'),
            'id': m.group('id'),
            'query': parse_js_string(m.group('query')),
            'title': parse_js_string(m.group('title')),
            'url': m.group('url'),
            'price': int(m.group('price')),
            'op': int(m.group('op')),
            'regDays': None if m.group('regDays') == 'null' else int(m.group('regDays')),
            'auth': None if m.group('auth') == 'null' else (m.group('auth') == 'true'),
        })
    return items


def load_fresh_for(source: Path) -> list[dict]:
    if not source.exists():
        return []
    data = json.loads(source.read_text(encoding='utf-8'))
    return data.get('data', [])


def normalize(entry: dict) -> dict:
    return {
        'date': entry['date'],
        'pl': entry['pl'],
        'id': entry['id'],
        'query': entry.get('query', ''),
        'title': entry.get('title', ''),
        'url': entry.get('url', ''),
        'price': int(entry.get('price') or 0),
        'op': int(entry.get('op') or 0),
        'regDays': entry.get('regDays'),
        'auth': entry.get('auth'),
    }


def format_record(r: dict) -> str:
    reg = 'null' if r['regDays'] is None else str(r['regDays'])
    if r['auth'] is None:
        auth = 'null'
    else:
        auth = 'true' if r['auth'] else 'false'
    return (
        "{"
        f"date:'{r['date']}',"
        f"pl:'{r['pl']}',"
        f"id:'{r['id']}',"
        f"query:'{js_string_escape(r['query'])}',"
        f"title:'{js_string_escape(r['title'])}',"
        f"url:'{r['url']}',"
        f"price:{r['price']},"
        f"op:{r['op']},"
        f"regDays:{reg},"
        f"auth:{auth}"
        "}"
    )


def main() -> int:
    html = DASHBOARD.read_text(encoding='utf-8')
    old = load_old_mon_raw(html)
    print(f'Старых записей: {len(old)}')

    # Собираем все «свежие» площадки из всех источников.
    # Платформа считается «свежей» только если в её источнике реально есть записи для неё.
    # Иначе мы оставим её из старого снапшота — иначе платформа потерялась бы при частичном
    # обновлении (например, парсили только Ozon — Avito не теряется).
    fresh_platforms_all = set()
    fresh_by_source: dict[Path, list[dict]] = {}
    for source, platforms in FRESH_PLATFORMS.items():
        items = [normalize(x) for x in load_fresh_for(source)]
        # фильтруем — берём только те платформы, что декларированы для источника
        items = [x for x in items if x['pl'] in platforms]
        fresh_by_source[source] = items
        # «свежими» считаем только те платформы, для которых реально есть данные
        present_pls = {x['pl'] for x in items}
        fresh_platforms_all.update(present_pls)
        label = source.name
        print(f'  {label}: {len(items)} записей ({", ".join(sorted(present_pls)) or "—"})')

    by_key = {}
    # Площадки без свежего источника — оставляем из старого снапшота.
    for r in old:
        if r['pl'] in fresh_platforms_all:
            continue
        by_key[(r['pl'], r['id'])] = r

    # Перекрываем свежими (в порядке источников)
    for items in fresh_by_source.values():
        for r in items:
            by_key[(r['pl'], r['id'])] = r

    merged = list(by_key.values())
    # Сортировка по умолчанию для дашборда:
    #   1) дисконт ↓ — сначала самые подозрительные карточки (99-100%)
    #   2) цена ↑ — внутри одного дисконта самые дешёвые приоритетнее (например
    #      99% от 39 990 ₽ = 13 ₽ — это явная цена-приманка)
    #   3) название ↑ — стабильный детерминированный порядок при равных
    #      дисконте и цене (одинаково для diff'а и для UI)
    def disc_pct(r):
        op = r.get('op') or 0
        if op <= 0:
            return 0
        return round((1 - r['price'] / op) * 100)
    merged.sort(key=lambda r: (-disc_pct(r), r['price'], r['title'].lower()))

    by_pl = {}
    for r in merged:
        by_pl.setdefault(r['pl'], 0)
        by_pl[r['pl']] += 1
    print('Итого по площадкам:', by_pl)
    print(f'Всего записей: {len(merged)}')

    # Формируем новый JS-литерал
    lines = ['    const MON_RAW = [']
    for r in merged:
        lines.append('        ' + format_record(r) + ',')
    # Убираем последнюю запятую — допустимо в JS, но не везде.
    if lines[-1].endswith(','):
        lines[-1] = lines[-1][:-1]
    lines.append('    ];')
    new_block = '\n'.join(lines)

    # Заменяем старый блок целиком
    start = html.find('    const MON_RAW = [')
    end = html.find('\n    ];', start)
    if start < 0 or end < 0:
        raise RuntimeError('Не нашёл границы MON_RAW для замены')
    end_full = end + len('\n    ];')
    new_html = html[:start] + new_block + html[end_full:]

    # Какие площадки получили свежие данные — указываем в комментарии
    fresh_label = ', '.join(sorted({p for p in fresh_platforms_all})) or 'нет'
    stale_pls = sorted({r['pl'] for r in old if r['pl'] not in fresh_platforms_all})
    stale_label = ', '.join(stale_pls) if stale_pls else 'нет'
    comment = (
        f"// {len(merged)} карточек мониторинга — свежий парсинг: {fresh_label}; "
        f"из прошлого снапшота: {stale_label}"
    )
    new_html = re.sub(
        r"// \d+ карточек из реального мониторинга[^\n]*", comment, new_html, count=1,
    )
    new_html = re.sub(
        r"// \d+ карточек мониторинга[^\n]*", comment, new_html, count=1,
    )

    # Обновим штамп «Последнее сканирование» в шапке раздела мониторинга
    now = datetime.now().strftime('%d.%m.%Y, %H:%M')
    new_html = re.sub(
        r'(<span id="scanLastTime"[^>]*>)[^<]*(</span>)',
        rf'\g<1>{now}\g<2>',
        new_html,
        count=1,
    )

    DASHBOARD.write_text(new_html, encoding='utf-8')
    print(f'Обновлён {DASHBOARD}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

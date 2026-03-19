#!/usr/bin/env python3
"""
compile_mon_raw.py
------------------
Читает 5 raw-файлов (avito_raw.txt, ym_raw.txt, wb_raw.txt, ozon_raw.txt, ale_raw.txt),
фильтрует и компилирует массив MON_RAW, патчит anti-ms-dashboard/index.html.

Формат raw-файлов: price|cardId|query|title  (UTF-8, одна запись на строку)

Запуск:
  python3 compile_mon_raw.py
  python3 compile_mon_raw.py --dashboard /path/to/index.html

Фильтрация:
  - цена 50-5000 руб.
  - title содержит ключевые слова office/microsoft/365/2021/2024/2019/ключ актив/лиценз
  - дедупликация по cardId
"""

import re
import sys
import os
from datetime import date

# ========== КОНФИГУРАЦИЯ ==========
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

SOURCES = [
    ('avito',       os.path.join(SCRIPT_DIR, 'avito_raw.txt')),
    ('yandex',      os.path.join(SCRIPT_DIR, 'ym_raw.txt')),
    ('wildberries', os.path.join(SCRIPT_DIR, 'wb_raw.txt')),
    ('ozon',        os.path.join(SCRIPT_DIR, 'ozon_raw.txt')),
    ('aliexpress',  os.path.join(SCRIPT_DIR, 'ale_raw.txt')),
]

DASHBOARD_HTML = os.path.join(PROJECT_DIR, 'anti-ms-dashboard', 'index.html')
SNIPPET_OUT    = os.path.join(SCRIPT_DIR, 'mon_raw_snippet.js')

PRICE_MIN = 50
PRICE_MAX = 5000

OFFICIAL_PRICES = {
    'office365':       6990,
    'office2021home':  14990,
    'office2021homebus': 22990,
    'windows11home':   13990,
    'windows11pro':    16990,
    'windows10':       13990,
    'default':         9990,
}

# Правило: title должен содержать 'office' ИЛИ комбинацию microsoft+365/офис.
# Чистые Windows, Visio, Project, чужое ПО - исключаются.
# Windows допустим только в связке с Office (напр. "Windows+Office ключ").

# ========== УТИЛИТЫ ==========

def detect_op(title: str) -> int:
    """Определяет официальную розничную цену по названию товара."""
    t = title.lower()
    if '365' in t or 'personal' in t or 'подписк' in t:
        return OFFICIAL_PRICES['office365']
    if 'home and business' in t or 'home & business' in t:
        return OFFICIAL_PRICES['office2021homebus']
    if '2021' in t and 'home' in t:
        return OFFICIAL_PRICES['office2021home']
    if '2021' in t:
        return OFFICIAL_PRICES['office2021home']
    if 'windows 11' in t and 'pro' in t:
        return OFFICIAL_PRICES['windows11pro']
    if 'windows 11' in t:
        return OFFICIAL_PRICES['windows11home']
    if 'windows 10' in t:
        return OFFICIAL_PRICES['windows10']
    return OFFICIAL_PRICES['default']


def make_id(pl: str, idx: int, query: str) -> str:
    """
    Генерирует внутренний ID записи (совместим с scrape-marketplaces.js):
    PL-{qhash}{idx:04}
    """
    prefixes = {'ozon': 'OZ', 'wildberries': 'WB', 'yandex': 'YM', 'avito': 'AV', 'aliexpress': 'AL'}
    qhash = 0
    for c in query:
        qhash = (qhash * 31 + ord(c)) & 0x7FFFFFFF
    qhash = qhash % 10000
    return f"{prefixes.get(pl, 'XX')}-{qhash}{str(idx).zfill(4)}"


def make_url(pl: str, card_id: str) -> str:
    """Строит URL карточки по платформе и cardId."""
    if pl == 'avito':
        return f'https://www.avito.ru/items/{card_id}'
    elif pl == 'yandex':
        return f'https://market.yandex.ru/product/{card_id}'
    elif pl == 'wildberries':
        return f'https://www.wildberries.ru/catalog/{card_id}/detail.aspx'
    elif pl == 'ozon':
        return f'https://www.ozon.ru/product/-{card_id}/'
    elif pl == 'aliexpress':
        return f'https://aliexpress.ru/item/{card_id}.html'
    return ''


def title_ok(title: str) -> bool:
    """
    Возвращает True только для Microsoft Office.
    Windows допустим если упомянут вместе с Office (бандл).
    Чужое ПО (Celemony, МойОфис, Visio, Project и т.д.) - исключается.
    """
    t = title.lower()
    has_ms_office = (
        'office' in t or
        ('365' in t and 'microsoft' in t) or
        ('офис' in t and 'microsoft' in t)
    )
    return has_ms_office


def js_str(s: str) -> str:
    return "'" + s.replace('\\', '\\\\').replace("'", "\\'") + "'"


# ========== ОСНОВНАЯ ЛОГИКА ==========

def compile_raw(dashboard_html: str = DASHBOARD_HTML) -> None:
    today = date.today().isoformat()
    entries = []
    seen_ids: set = set()
    counters: dict = {}

    for pl, path in SOURCES:
        counters[pl] = 0
        if not os.path.exists(path):
            print(f"  [WARN] Файл не найден: {path}")
            continue
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|', 3)
                if len(parts) != 4:
                    continue
                price_s, card_id, query, title = parts
                try:
                    price = int(price_s)
                except ValueError:
                    continue

                # Фильтры
                if price < PRICE_MIN or price > PRICE_MAX:
                    continue
                if not title_ok(title):
                    continue
                if card_id in seen_ids:
                    continue
                seen_ids.add(card_id)

                op = detect_op(title)
                idx = len(entries)
                entries.append({
                    'date':    today,
                    'pl':      pl,
                    'id':      make_id(pl, idx, query),
                    'query':   query,
                    'title':   title,
                    'url':     make_url(pl, card_id),
                    'price':   price,
                    'op':      op,
                    'regDays': 'null',
                    'auth':    'null',
                })
                counters[pl] += 1

    print(f"Всего записей после фильтрации: {len(entries)}")
    for pl, cnt in counters.items():
        print(f"  {pl}: {cnt}")

    # Строим JS-сниппет
    lines_js = ['    const MON_RAW = [']
    for e in entries:
        lines_js.append(
            f"        {{date:{js_str(e['date'])},pl:{js_str(e['pl'])},id:{js_str(e['id'])},"
            f"query:{js_str(e['query'])},title:{js_str(e['title'])},url:{js_str(e['url'])},"
            f"price:{e['price']},op:{e['op']},regDays:{e['regDays']},auth:{e['auth']}}},"
        )
    lines_js.append('    ];')
    snippet = '\n'.join(lines_js)

    # Сохраняем сниппет
    with open(SNIPPET_OUT, 'w', encoding='utf-8') as f:
        f.write(snippet)
    print(f"Сохранён {SNIPPET_OUT} ({len(snippet)} байт)")

    # Патчим index.html
    if not os.path.exists(dashboard_html):
        print(f"  [WARN] Dashboard не найден: {dashboard_html}")
        return

    with open(dashboard_html, encoding='utf-8') as f:
        html = f.read()

    START_MARKER = '    const MON_RAW = ['
    END_MARKER   = '    ];'

    start_idx = html.find(START_MARKER)
    if start_idx == -1:
        print("  [ERROR] Маркер 'const MON_RAW = [' не найден в HTML!")
        return

    end_idx = html.find(END_MARKER, start_idx)
    if end_idx == -1:
        print("  [ERROR] Маркер '    ];' не найден после MON_RAW!")
        return
    end_idx += len(END_MARKER)

    patched = html[:start_idx] + snippet + '\n' + html[end_idx:]
    with open(dashboard_html, 'w', encoding='utf-8') as f:
        f.write(patched)
    print(f"Обновлён {dashboard_html} (было {len(html)}, стало {len(patched)} байт)")


if __name__ == '__main__':
    # Опциональный аргумент --dashboard /path/to/index.html
    dash = DASHBOARD_HTML
    for i, arg in enumerate(sys.argv[1:]):
        if arg == '--dashboard' and i + 1 < len(sys.argv[1:]):
            dash = sys.argv[i + 2]
    compile_raw(dash)

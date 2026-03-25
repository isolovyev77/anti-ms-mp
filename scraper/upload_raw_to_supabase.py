#!/usr/bin/env python3
"""
upload_raw_to_supabase.py
--------------------------
Читает raw-файлы (ym_raw.txt, avito_raw.txt, wb_raw.txt, ale_raw.txt),
фильтрует, трансформирует и загружает в Supabase таблицу listings через REST API.

product_id формат: {PREFIX}-{cardId} (WB-130635707, YM-xep3ubtl9ng и т.д.)
Для Ozon данные уже загружены отдельно.
"""

import json
import os
import subprocess
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SUPABASE_URL = "https://yqfdbuiyfkzhkhpiknob.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlxZmRidWl5Zmt6aGtocGlrbm9iIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQxMDY5MzYsImV4cCI6MjA4OTY4MjkzNn0.k1L6IBj2Xe8Zq5pMDBrtAbLUAQ6byG2yI5iD_-qcdlM"

SOURCES = [
    ('yandex',      'YM',  os.path.join(SCRIPT_DIR, 'ym_raw.txt')),
    ('avito',       'AV',  os.path.join(SCRIPT_DIR, 'avito_raw.txt')),
    ('wildberries', 'WB',  os.path.join(SCRIPT_DIR, 'wb_raw.txt')),
    ('aliexpress',  'AL',  os.path.join(SCRIPT_DIR, 'ale_raw.txt')),
]

PRICE_MIN = 50
PRICE_MAX = 5000

OFFICIAL_PRICES = {
    'office365': 6990, 'office2021home': 14990, 'office2021homebus': 22990,
    'windows11home': 13990, 'windows11pro': 16990, 'windows10': 13990, 'default': 9990,
}

def detect_op(title):
    t = title.lower()
    if '365' in t or 'personal' in t or 'подписк' in t: return OFFICIAL_PRICES['office365']
    if 'home and business' in t or 'home & business' in t: return OFFICIAL_PRICES['office2021homebus']
    if '2021' in t: return OFFICIAL_PRICES['office2021home']
    if 'windows 11' in t and 'pro' in t: return OFFICIAL_PRICES['windows11pro']
    if 'windows 11' in t: return OFFICIAL_PRICES['windows11home']
    if 'windows 10' in t: return OFFICIAL_PRICES['windows10']
    return OFFICIAL_PRICES['default']

def title_ok(title):
    t = title.lower()
    if 'officesuite' in t: return False
    return 'office' in t or ('365' in t and 'microsoft' in t)

def make_url(pl, card_id):
    if pl == 'avito':      return f'https://www.avito.ru/items/{card_id}'
    if pl == 'yandex':     return f'https://market.yandex.ru/product/{card_id}'
    if pl == 'wildberries':return f'https://www.wildberries.ru/catalog/{card_id}/detail.aspx'
    if pl == 'aliexpress': return f'https://aliexpress.ru/item/{card_id}.html'
    return ''

def upload_batch(records):
    """Загружает батч через curl (upsert по product_id)."""
    payload = json.dumps(records)
    result = subprocess.run([
        'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
        '-X', 'POST',
        f'{SUPABASE_URL}/rest/v1/listings',
        '-H', f'apikey: {SUPABASE_KEY}',
        '-H', f'Authorization: Bearer {SUPABASE_KEY}',
        '-H', 'Content-Type: application/json',
        '-H', 'Prefer: resolution=merge-duplicates',
        '-d', payload
    ], capture_output=True, text=True)
    return result.stdout.strip()

today = date.today().isoformat()
all_records = []
counters = {}

for pl, prefix, filepath in SOURCES:
    if not os.path.exists(filepath):
        print(f"  [SKIP] Нет файла: {filepath}")
        continue

    records = []
    seen = set()
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            parts = line.split('|', 3)
            if len(parts) != 4: continue
            price_s, card_id, query, title = parts
            try:
                price = int(price_s)
            except ValueError:
                continue
            if price < PRICE_MIN or price > PRICE_MAX: continue
            if not title_ok(title): continue
            if card_id in seen: continue
            seen.add(card_id)

            records.append({
                'date':       today,
                'pl':         pl,
                'product_id': f'{prefix}-{card_id}',
                'query':      query,
                'title':      title[:120],
                'url':        make_url(pl, card_id),
                'price':      price,
                'op':         detect_op(title),
            })

    counters[pl] = len(records)
    all_records.extend(records)
    print(f"  {pl}: {len(records)} записей после фильтрации")

print(f"\nВсего: {sum(counters.values())} записей")
print("Загружаю батчами по 200...")

BATCH = 200
total_ok = 0
for i in range(0, len(all_records), BATCH):
    batch = all_records[i:i+BATCH]
    code = upload_batch(batch)
    status = "OK" if code in ('200', '201') else f"ERR {code}"
    print(f"  Батч {i//BATCH + 1} ({len(batch)} записей): {status}")
    if code in ('200', '201'):
        total_ok += len(batch)

print(f"\nУспешно загружено: {total_ok} из {len(all_records)}")

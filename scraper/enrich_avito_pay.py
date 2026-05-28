#!/usr/bin/env python3
"""Enrichment: для каждой Avito-карточки из mon_data_captcha.json определить,
поддерживает ли объявление покупку через инфраструктуру Avito (Авито Доставка /
безопасная сделка / оплата на счёт Avito).

Юридический смысл: Avito заявляет, что является доской объявлений и несёт
ответственность только за объявления с оплатой через её инфраструктуру.
Карточка с кнопкой «Купить с доставкой» = зона ответственности Avito.

Признак (для ПОКУПАТЕЛЯ, не приглашение продавцу):
  на странице товара есть кнопка покупки/заказа через Avito —
  «Купить с доставкой» / «Заказать с доставкой» / «Добавить в корзину» /
  «Купить сейчас» / «Оформить заказ».
  Если только «Написать» / «Показать телефон» — оплата идёт ВНЕ Avito.

Запуск:
  ssh -D 1080 -fN -i ~/.ssh/VDSina root@94.103.89.251
  SCRAPER_PROXY=socks5://127.0.0.1:1080 .venv/bin/python enrich_avito_pay.py

Результат: avito_pay.json — { "AV-12345": true/false, ... }
Промежуточно сохраняется каждые 20 карточек (на случай краха).
"""
import json
import os
import random
import sys
import time
from pathlib import Path

from cloakbrowser import launch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'scraper' / 'mon_data_captcha.json'
OUT = ROOT / 'cloakbrowser-lab' / 'avito_pay.json'
PROXY = os.environ.get('SCRAPER_PROXY')

DETECT_JS = r"""
() => {
  const body = document.body.innerText || '';
  // Реальная кнопка покупки через Avito (для покупателя)
  const canBuy = /Купить с доставкой|Заказать с доставкой|Добавить в корзину|Купить сейчас|Оформить заказ|Купить с Авито Доставкой/i.test(body);
  // Блок безопасной сделки активен (не пустой)
  const sd = document.querySelector('[data-marker="safedeal-item-header"]');
  const safedealActive = !!(sd && sd.innerText && sd.innerText.trim().length > 0);
  // Заблокировано / удалено
  const blocked = /Доступ ограничен|Объявление снято|больше не доступно/i.test(body.slice(0,300));
  return { canBuy, safedealActive, blocked };
}
"""


def main() -> int:
    data = json.loads(SRC.read_text(encoding='utf-8'))
    avito = [x for x in data['data'] if x['pl'] == 'avito']
    print(f'Avito карточек для проверки: {len(avito)}')

    # Возобновление: подхватываем уже проверенные
    done = {}
    if OUT.exists():
        try:
            done = json.loads(OUT.read_text(encoding='utf-8'))
            print(f'Уже проверено ранее: {len(done)}')
        except Exception:
            done = {}

    # Перепроверяем карточки, по которым результат True/False уже есть.
    # None (блок/ошибка) и отсутствие — в очередь на (пере)проверку.
    todo = [x for x in avito if done.get(x['id']) is None]
    print(f'Осталось проверить (вкл. ранее заблокированные): {len(todo)}')
    if not todo:
        print('Всё уже проверено.')
        return 0

    kw = {'headless': True, 'humanize': True}
    if PROXY:
        kw['proxy'] = PROXY
    browser = launch(**kw)
    page = browser.new_page()

    blocked_streak = 0
    for i, card in enumerate(todo):
        url = card['url'].split('?')[0]
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=40000)
            time.sleep(random.uniform(2.5, 5.0))  # человекоподобная пауза против бана
            page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            time.sleep(random.uniform(0.6, 1.4))
            info = page.evaluate(DETECT_JS)
            if info.get('blocked'):
                blocked_streak += 1
                done[card['id']] = None  # не смогли проверить
                if blocked_streak >= 5:
                    print('5 блокировок подряд — пауза 60 сек и продолжаю')
                    OUT.write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding='utf-8')
                    time.sleep(60)
                    blocked_streak = 0
            else:
                blocked_streak = 0
                done[card['id']] = bool(info.get('canBuy'))
        except Exception as e:
            done[card['id']] = None
            print(f'  {i+1} ERR {str(e)[:60]}')
        # Прогресс + промежуточное сохранение
        if (i + 1) % 20 == 0:
            yes = sum(1 for v in done.values() if v is True)
            OUT.write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'  {i+1}/{len(todo)} проверено, с оплатой Avito: {yes}')

    browser.close()
    OUT.write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding='utf-8')
    yes = sum(1 for v in done.values() if v is True)
    no = sum(1 for v in done.values() if v is False)
    unk = sum(1 for v in done.values() if v is None)
    print(f'\nГОТОВО: {len(done)} карточек')
    print(f'  с оплатой через Avito (apay=true): {yes}')
    print(f'  без оплаты (написать/телефон): {no}')
    print(f'  не проверено (блок/ошибка): {unk}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

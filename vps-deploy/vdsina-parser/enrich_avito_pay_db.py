#!/usr/bin/env python3
"""Enrichment avito_pay в Supabase: определяет, продаётся ли Avito-карточка
с оплатой через инфраструктуру Avito (Авито Доставка / безопасная сделка).

Полное правило: scraper/AVITO_PAY_DETECTION.md
Юр.смысл: apay=true → прямая зона ответственности Avito (можно купить через них).

Логика:
1. Берёт listings WHERE pl='avito' AND avito_pay IS NULL (свежие, last_seen недавний),
   максимум BATCH=200 за прогон.
2. Заходит на страницу каждой карточки через cloakbrowser, применяет DETECT_JS.
3. Проставляет avito_pay (true/false) + avito_pay_checked_at в Supabase.
4. Антибан: паузы 2.5-5с, cooldown 60с после 5 блокировок подряд.

Запуск (cron, НЕ пересекаясь с parser_runner по cloakbrowser):
  30 */6 * * * cd /opt/anti-ms-mp && .venv/bin/python enrich_avito_pay_db.py >> /var/log/anti-ms-apay.log 2>&1

ENV: тот же .env (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SCRAPER_PROXY).
"""
import datetime as dt
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from cloakbrowser import launch

BASE = Path(__file__).parent
env_path = BASE / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SCRAPER_PROXY = os.environ.get("SCRAPER_PROXY", "")
BATCH = int(os.environ.get("APAY_BATCH", "200"))

DETECT_JS = r"""
() => {
  const body = document.body.innerText || '';
  const canBuy = /Купить с доставкой|Заказать с доставкой|Добавить в корзину|Купить сейчас|Оформить заказ|Купить с Авито Доставкой/i.test(body);
  const sd = document.querySelector('[data-marker="safedeal-item-header"]');
  const safedealActive = !!(sd && sd.innerText && sd.innerText.trim().length > 0);
  const blocked = /Доступ ограничен|Объявление снято|больше не доступно/i.test(body.slice(0,300));
  return { canBuy, safedealActive, blocked };
}
"""


def log(msg, **kw):
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    extra = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[{ts}] [apay] {msg} {extra}".rstrip(), flush=True)


def sb_get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def sb_patch(path, payload):
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        data=json.dumps(payload).encode(),
        method="PATCH",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=15).read()


def parser_running() -> bool:
    """Не запускаемся параллельно с основным парсером (cloakbrowser не делится)."""
    try:
        out = subprocess.run(["pgrep", "-f", "parser_runner.py"],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and out.stdout.strip() != ""
    except Exception:
        return False


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("config missing"); return 1
    if parser_running():
        log("parser_runner работает — откладываю enrichment"); return 0

    # Непроверенные avito-карточки, свежие (есть смысл проверять только живые)
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).date().isoformat()
    rows = sb_get(
        f"/rest/v1/listings?select=pl,product_id,url"
        f"&pl=eq.avito&avito_pay=is.null&last_seen=gte.{cutoff}"
        f"&order=last_seen.desc&limit={BATCH}"
    )
    log("к проверке", count=len(rows))
    if not rows:
        log("нет непроверенных avito-карточек"); return 0

    kw = {"headless": True, "humanize": True}
    if SCRAPER_PROXY:
        kw["proxy"] = SCRAPER_PROXY
    browser = launch(**kw)
    page = browser.new_page()
    try:
        page.set_default_navigation_timeout(40000)
        page.set_default_timeout(30000)
    except Exception:
        pass

    yes = no = unk = 0
    blocked_streak = 0
    for i, r in enumerate(rows):
        pid, url = r["product_id"], (r.get("url") or "")
        if not url:
            continue
        url = url.split("?")[0]
        result = None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            time.sleep(random.uniform(2.5, 5.0))
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 1.4))
            info = page.evaluate(DETECT_JS)
            if info.get("blocked"):
                blocked_streak += 1
                unk += 1
                if blocked_streak >= 5:
                    log("5 блокировок подряд — cooldown 60с")
                    time.sleep(60)
                    blocked_streak = 0
            else:
                blocked_streak = 0
                result = bool(info.get("canBuy"))
        except Exception as e:
            unk += 1
            log("ERR", i=i+1, err=str(e)[:80])

        if result is not None:
            try:
                sb_patch(
                    f"/rest/v1/listings?pl=eq.avito&product_id=eq.{pid}",
                    {"avito_pay": result,
                     "avito_pay_checked_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                )
                if result: yes += 1
                else: no += 1
            except Exception as e:
                log("patch FAIL", pid=pid, err=str(e)[:80])

        if (i + 1) % 20 == 0:
            log("прогресс", done=i+1, total=len(rows), apay_yes=yes, apay_no=no, unchecked=unk)

    browser.close()
    log("ГОТОВО", checked=len(rows), apay_yes=yes, apay_no=no, unchecked=unk)
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()

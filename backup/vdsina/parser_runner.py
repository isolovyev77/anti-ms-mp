#!/usr/bin/env python3
"""
Парсер маркетплейсов для VDSina — заменяет старый Node.js scrape-marketplaces.js.

Запускается из cron каждые 6 часов:
  0 */6 * * * cd /opt/anti-ms-mp && .venv/bin/python parser_runner.py >> /var/log/anti-ms.log 2>&1

Также можно дёрнуть ручную сессию через n8n webhook → ssh:
  ssh root@vdsina 'cd /opt/anti-ms-mp && nohup .venv/bin/python parser_runner.py --trigger manual &'

ENV (.env):
  SUPABASE_URL=https://yqfdbuiyfkzhkhpiknob.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=<сюда service_role key>
  SCRAPER_PROXY=          (пусто = напрямую с VDSina)
  N8N_WEBHOOK_PARSER_DONE=https://.../webhook/parser-done

Записывает в Supabase:
  - listings (UPSERT по pl,product_id)
  - parser_runs (одна запись со статусом и счётчиками)
"""
import argparse
import datetime as dt
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from cloakbrowser import launch
from extractors import (PLATFORMS, OFFICIAL_PRICES, EXTRACT_JS,
                        BLOCK_MARKERS, title_ok, official_price)

# === Конфиг ===
# QUERIES теперь читаются из Supabase (таблица monitor_queries, active=true).
# Этот список — fallback на случай, если БД недоступна (см. load_queries()).
# Снимок проверенного рабочего набора (= активные monitor_queries на 2026-05-30).
# Статический: используется ТОЛЬКО при недоступности БД. Не авто-синхронизируется
# с дашбордом — при сильном изменении набора пере-синхронизировать вручную.
QUERIES_FALLBACK = [
    "Office ключ",          # широкий базовый (включает MS Office ключ, Office 2021/2024 ключ)
    "Office активация",     # альтернативная формулировка
    "Microsoft 365",        # подписочный сегмент (отдельный от ключей)
    "Office LTSC",          # корпоративные бессрочные
    "Office 2024",          # новейшая версия без слов «ключ/активация»
    "Office 2019",          # старая популярная версия
    "Майкрософт Офис 2021", # кириллица — отдельный сегмент (+15% новых при тесте)
    "Майкрософт Офис 2016", # кириллица — старая версия (+24% новых при тесте)
]
# Заполняется в main() из load_queries(); глобал, т.к. scrape_one_platform читает его.
QUERIES: list[str] = list(QUERIES_FALLBACK)


# === ENV ===
def env(key, default=None, required=False):
    v = os.environ.get(key, default)
    if required and not v:
        sys.exit(f"ENV {key} обязателен")
    return v

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

load_env_file(Path(__file__).parent / ".env")
SUPABASE_URL = env("SUPABASE_URL", required=True)
SUPABASE_SERVICE_ROLE_KEY = env("SUPABASE_SERVICE_ROLE_KEY", required=True)
SCRAPER_PROXY = env("SCRAPER_PROXY", "").strip() or None
N8N_WEBHOOK = env("N8N_WEBHOOK_PARSER_DONE", "").strip() or None

# Поведение при антибот-блоке (DataDome). Подобрано под паттерн Ozon: CloakBrowser
# метится по velocity после ~1 запроса с дата-центрового IP, дальше всё летит в блок.
QUERY_DELAY_LO   = float(env("QUERY_DELAY_LO", "2.0"))   # пауза между запросами, сек (низ)
QUERY_DELAY_HI   = float(env("QUERY_DELAY_HI", "5.0"))   # пауза между запросами, сек (верх); 0 = выкл
BLOCK_BACKOFF_S  = float(env("BLOCK_BACKOFF_S", "60"))   # однократный cooldown при первом блоке площадки; 0 = выкл
BLOCK_BAIL_AFTER = int(env("BLOCK_BAIL_AFTER", "2"))     # после N заблокированных запросов подряд — стоп CloakBrowser (добёрет Camoufox); 0 = выкл

# Прокси-ступень каскада: при блоке площадки на ПРЯМОМ IP VDSina — повторный проход
# CloakBrowser через РФ-прокси (свежий IP, напр. RuVDS-туннель) ДО Camoufox. Другой
# РФ-IP с низким velocity часто проходит там, где прямой флагнут DataDome. Per-platform.
OZON_PROXY            = env("OZON_PROXY", "").strip() or None   # socks5://127.0.0.1:1081 (RuVDS); пусто = ступень выкл
CLOAK_PROXY_FALLBACK  = os.environ.get("CLOAK_PROXY_FALLBACK", "1") != "0"
CLOAK_PROXY_MAX_PAGES = int(os.environ.get("CLOAK_PROXY_MAX_PAGES", "3"))
PLATFORM_PROXY        = {"ozon": OZON_PROXY}                     # площадка → прокси для ступени (None = выкл)

# === Утилиты ===
# Время последнего лог-события = реальный прогресс скрейпа. heartbeat шлёт ЕГО,
# а не now(), чтобы watchdog ловил зависания по остановке прогресса (за ~5 мин),
# а не только по возрасту процесса (45 мин). При зависании log() не вызывается →
# heartbeat «замирает» → watchdog убивает.
_LAST_PROGRESS = {"ts": dt.datetime.now(dt.timezone.utc)}

def log(msg: str, **fields) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    _LAST_PROGRESS["ts"] = now
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{now.isoformat()}] {msg} {extras}", flush=True)

def jitter(lo=1.0, hi=2.5):
    time.sleep(random.uniform(lo, hi))

# detect_official_price удалён — используем official_price() из 8-типной логики выше

def supabase_post(path: str, payload, method="POST", prefer=None) -> dict | None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(f"{SUPABASE_URL}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        log(f"supabase {method} {path} FAIL", code=e.code, body=e.read()[:300])
        raise

def load_queries() -> list[str]:
    """Читает активные запросы из monitor_queries. При ошибке — QUERIES_FALLBACK."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/monitor_queries?select=query&active=eq.true&order=id",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        qs = [row["query"] for row in rows if row.get("query")]
        if qs:
            return qs
        log("load_queries: пустой список в БД, использую fallback")
    except Exception as e:
        log("load_queries FAIL, использую fallback", err=str(e)[:200])
    return list(QUERIES_FALLBACK)


def is_query_active(q: str) -> bool:
    """Активен ли запрос в monitor_queries СЕЙЧАС. Запрос мог быть удалён с дашборда
    (remove_queries) за время прогона — тогда писать его карточки нельзя (осиротеют).
    Fail-open: при ошибке проверки считаем активным, чтобы сбой Supabase не терял данные."""
    try:
        from urllib.parse import quote as _q
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/monitor_queries?query=eq.{_q(q, safe='')}&active=eq.true&select=query&limit=1",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return len(json.loads(r.read() or "[]")) > 0
    except Exception as e:
        log("is_query_active FAIL — считаем активным (fail-open)", err=str(e)[:150])
        return True


def mark_query_parsed(query: str) -> None:
    """Обновляет last_parsed_at для запроса. Best-effort."""
    try:
        payload = json.dumps({"last_parsed_at": dt.datetime.now(dt.timezone.utc).isoformat()}).encode()
        from urllib.parse import quote as _q
        req = urllib.request.Request(
            # safe='' — как в is_query_active; иначе '/' в запросе не экранируется и
            # PostgREST-фильтр query=eq.… не находит строку (тихий промах PATCH).
            f"{SUPABASE_URL}/rest/v1/monitor_queries?query=eq.{_q(query, safe='')}",
            data=payload, method="PATCH",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def n8n_notify(payload: dict) -> None:
    if not N8N_WEBHOOK:
        return
    try:
        req = urllib.request.Request(
            N8N_WEBHOOK,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log("n8n notify failed (продолжаю)", err=str(e)[:200])

# === Извлечение карточек ===


def detect_block(page) -> tuple[bool, str]:
    sample = page.evaluate("() => (document.body.innerText || '').slice(0, 400) + ' ' + document.title")
    lower = sample.lower()
    found = [m for m in BLOCK_MARKERS if m in lower]
    return bool(found), sample[:200]


def scroll_lazy(page, rounds=4):
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(random.uniform(0.6, 1.2))
        except Exception:
            break


def _safe_detect_block(page) -> tuple[bool, str]:
    """detect_block может бросить ExecCtx-исключение если страница в момент проверки
    делает SPA-навигацию (типично для avito antibot-redirect). Маскируем как 'blocked'."""
    try:
        return detect_block(page)
    except Exception as e:
        return True, f"ExecCtx during detect_block: {str(e)[:120]}"


def _scrape_one_page(page, platform: str, q: str, p: int, run_log, items: list[dict],
                     errors: list[dict]) -> tuple[bool, bool]:
    """Скрейпит одну страницу. Возвращает (page_ok, should_break_query).

    Любая ошибка ловится тут локально, чтобы не оборвать весь прогон платформы.
    """
    cfg = PLATFORMS[platform]
    url = cfg["url"](q, p)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        errors.append({"platform": platform, "query": q, "page": p, "message": f"goto: {str(e)[:120]}"})
        return False, True
    time.sleep(2)
    # Дожидаемся успокоения сети — анти-SPA-навигация защита.
    # Не критично, если не дождались за 8 сек — продолжаем.
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    blocked, sample = _safe_detect_block(page)
    if blocked:
        errors.append({"platform": platform, "query": q, "page": p, "message": f"BLOCKED: {sample[:120]}"})
        return False, True
    scroll_lazy(page)
    try:
        cards = page.evaluate(EXTRACT_JS[platform])
    except Exception as e:
        errors.append({"platform": platform, "query": q, "page": p, "message": f"extract: {str(e)[:120]}"})
        return False, False  # это страница упала, но другие query на этой платформе можно пробовать
    if not cards:
        return True, True
    kept = 0
    for c in cards:
        title = (c.get("title") or "").strip()
        if not title or not title_ok(title, c.get("url") or ""):
            continue
        c["platform"] = platform
        c["query"] = q
        items.append(c)
        kept += 1
    run_log(f"    стр.{p}: {len(cards)} → {kept} после title_ok (итого {len(items)})")
    return True, False


def scrape_one_platform(page, platform: str, run_log) -> tuple[list[dict], list[dict]]:
    """Скрейпит платформу. Любая ошибка отдельной query/page изолируется —
    остальные query пробуются, чтобы не потерять всю платформу из-за одной
    кривой страницы (типично avito SPA-navigation: "Execution context was destroyed")."""
    cfg = PLATFORMS[platform]
    items: list[dict] = []
    errors: list[dict] = []
    consecutive_blocks = 0     # запросов подряд, упёршихся в антибот
    did_backoff = False        # cooldown — один раз на площадку
    queries = list(QUERIES)
    for qi, q in enumerate(queries):
        run_log(f"  [{platform}] query='{q}'")
        errs_before = len(errors)
        for p in range(1, cfg["max_pages"] + 1):
            page_ok, should_break = _scrape_one_page(page, platform, q, p, run_log, items, errors)
            # Retry стр.1 один раз при ExecCtx ошибке — частый паттерн avito antibot
            if not page_ok and p == 1 and errors and "extract:" in (errors[-1].get("message") or ""):
                run_log(f"    стр.1 retry после extract-ошибки")
                errors.pop()  # убрать запись о первой попытке: при успешном retry не раздуваем errors→partial; при повторном сбое _scrape_one_page добавит свежую
                time.sleep(3)
                page_ok, should_break = _scrape_one_page(page, platform, q, p, run_log, items, errors)
            if should_break:
                break
            jitter()
        # Был ли этот запрос заблокирован антиботом?
        blocked_this_query = any("BLOCKED" in (e.get("message") or "") for e in errors[errs_before:])
        if blocked_this_query:
            consecutive_blocks += 1
            # Однократный cooldown — дать velocity-счётчику антибота остыть.
            # (heartbeat шлёт отдельный таймер-поток каждые 30с → watchdog не сработает.)
            if not did_backoff and BLOCK_BACKOFF_S > 0:
                run_log(f"    [{platform}] антибот-блок → пауза {int(BLOCK_BACKOFF_S)}с")
                time.sleep(BLOCK_BACKOFF_S)
                did_backoff = True
            # IP помечен — дальше CloakBrowser бесполезен и лишь держит метку.
            # Camoufox-fallback (триггер: BLOCKED в errors) проходит ВСЕ запросы сам,
            # поэтому оставшиеся после раннего выхода запросы не теряются.
            if CAMOUFOX_FALLBACK and BLOCK_BAIL_AFTER > 0 and consecutive_blocks >= BLOCK_BAIL_AFTER:
                run_log(f"    [{platform}] {consecutive_blocks} запросов подряд блок → стоп CloakBrowser, добор Camoufox")
                break
        else:
            consecutive_blocks = 0
        # Профилактика: пауза между запросами, чтобы не триггерить velocity-эвристику.
        if qi < len(queries) - 1 and QUERY_DELAY_HI > 0:
            time.sleep(random.uniform(QUERY_DELAY_LO, QUERY_DELAY_HI))
    return items, errors


def dedup(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        k = (it["platform"], it["id"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _count_from_content_range(resp) -> int:
    """Число затронутых строк из заголовка Content-Range PostgREST (count=exact).

    Формат «0-9/25» или «*/25» → 25. Если заголовка нет — 0 (удаление всё равно
    прошло, счётчик нужен лишь для лога).
    """
    cr = resp.headers.get("Content-Range", "") or ""
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    return 0


def cleanup_old_listings(retention_days: int = 14) -> int:
    """Удалить «исчезнувшие» с маркетплейсов карточки (last_seen старше N дней).

    Запускается после каждого cron-прогона. Не блокирующая — при ошибке
    логируем и продолжаем (парсинг важнее retention).

    Возвращает количество удалённых строк (или -1 при ошибке).
    """
    cutoff = (dt.date.today() - dt.timedelta(days=retention_days)).isoformat()
    try:
        # DELETE через PostgREST: last_seen < cutoff
        # PostgREST DELETE требует фильтра — используем lt (less than)
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/listings?last_seen=lt.{cutoff}",
            method="DELETE",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Prefer": "return=minimal,count=exact",  # #34: счётчик в заголовке, без тела
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()  # тело пустое (return=minimal) — дренируем соединение
            return _count_from_content_range(r)
    except Exception as e:
        log("cleanup failed (не критично)", err=str(e)[:200])
        return -1


def cleanup_old_parser_runs(retention_days: int = 90) -> int:
    """Удалить parser_runs старше N дней. Тоже не блокирующая."""
    # strftime без таймзоны: .isoformat() даёт "...+00:00", а '+' в URL читается
    # как пробел → PostgREST 400 (фильтр невалиден). Из-за этого чистка parser_runs
    # раньше молча падала и таблица росла. Naive-timestamp PostgREST принимает.
    cutoff_dt = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/parser_runs?started_at=lt.{cutoff_dt}",
            method="DELETE",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Prefer": "return=minimal,count=exact",  # #34
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
            return _count_from_content_range(r)
    except Exception as e:
        log("cleanup parser_runs failed (не критично)", err=str(e)[:200])
        return -1


def cleanup_orphan_listings_rpc(days: int = 7) -> int:
    """Удалить карточки «осиротевших» запросов (которых больше нет среди активных
    monitor_queries) старше `days` дней — через RPC cleanup_orphan_listings (PostgREST
    не умеет DELETE с подзапросом). Защита от разрастания БД, когда запрос удалили без
    галочки «удалить карточки». Best-effort."""
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/rpc/cleanup_orphan_listings",
            data=json.dumps({"p_days": days}).encode(),
            method="POST",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return int(json.loads(body)) if body else 0
    except Exception as e:
        log("cleanup_orphan failed (не критично)", err=str(e)[:200])
        return -1


def upsert_listings(rows: list[dict]) -> int:
    if not rows:
        return 0
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).date().isoformat()  # МСК-дата (согласовано с daily_stats и healthcheck)
    payload = [
        {
            "date": today,
            "pl": r["platform"],
            "product_id": r["id"],
            "query": r["query"],
            "title": r["title"][:500],
            "url": r.get("url"),
            "price": int(r["price"]) if r.get("price") else None,
            "op": official_price(r["title"]),
            # first_seen НЕ шлём: триггер listings_first_seen_trg ставит его при
            # ПЕРВОЙ вставке (= last_seen), а при повторном upsert (merge-duplicates)
            # отсутствие поля сохраняет исходную дату. Иначе first_seen затирался на
            # сегодня каждый прогон → ломались «Свежие» и «Новых за сутки».
            "last_seen": today,
        }
        for r in rows
    ]
    BATCH = 200
    ok = 0
    for i in range(0, len(payload), BATCH):
        batch = payload[i:i + BATCH]
        supabase_post(
            "/rest/v1/listings?on_conflict=pl,product_id",
            batch,
            method="POST",
            prefer="resolution=merge-duplicates,return=minimal",
        )
        ok += len(batch)
    return ok


def insert_parser_run(trigger: str) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    resp = supabase_post(
        "/rest/v1/parser_runs",
        {
            "status": "running",
            "trigger": trigger,
            "host": socket.gethostname(),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "last_heartbeat": now,
        },
        method="POST",
        prefer="return=representation",
    )
    return resp[0]["id"] if isinstance(resp, list) else resp["id"]


def heartbeat(run_id: int) -> None:
    """Раз в 30 сек пишем last_heartbeat = время последнего ПРОГРЕССА (лог-события),
    а не now(). Так при зависании heartbeat замирает и watchdog убивает прогон за
    ~5 мин (STALE_AFTER), а не ждёт 45-мин возрастной kill."""
    ts = _LAST_PROGRESS.get("ts") or dt.datetime.now(dt.timezone.utc)
    payload = json.dumps({"last_heartbeat": ts.isoformat()}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/parser_runs?id=eq.{run_id}",
        data=payload,
        method="PATCH",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass  # heartbeat best-effort; watchdog убьёт всё равно если важно


def start_heartbeat_thread(run_id: int, interval: int = 30) -> threading.Event:
    """Запускает фоновый поток, который каждые `interval` секунд пишет heartbeat.
    Возвращает Event, который надо `.set()` чтобы остановить поток."""
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            heartbeat(run_id)

    t = threading.Thread(target=loop, daemon=True, name=f"heartbeat-{run_id}")
    t.start()
    return stop


def finalize_parser_run(run_id: int, totals: dict, errors: list[dict], status: str) -> None:
    payload = {
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": status,
        "totals": totals,
        "errors": errors,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/parser_runs?id=eq.{run_id}",
        data=data,
        method="PATCH",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=20).read()


def upsert_daily_stats(rows: list[dict]) -> None:
    """Дневной снимок статистики в daily_stats (upsert по day,pl). Решает проблему
    схлопывания listings.last_seen — история динамики живёт здесь, не в листингах."""
    if not rows:
        return
    data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/daily_stats?on_conflict=day,pl",
        data=data,
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log("daily_stats upsert failed (не критично)", err=str(e)[:200])


def platform_daily_stat(plat: str, items_d: list[dict]) -> dict:
    """Считает дневной срез по платформе: всего, контрафакт, средний дисконт."""
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).date().isoformat()  # МСК-дата (как last_seen в upsert_listings и healthcheck)
    cf = []
    for c in items_d:
        price = c.get("price") or 0
        op = official_price(c.get("title") or "")
        if price > 0 and op > 0 and price < 0.5 * op:
            cf.append(round((1 - price / op) * 100))
    avg_disc = round(sum(cf) / len(cf)) if cf else None
    return {"day": today, "pl": plat, "total": len(items_d),
            "counterfeit": len(cf), "avg_disc": avg_disc,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


# === Запасной движок Camoufox (Firefox) ============================================
# При блокировке площадки CloakBrowser'ом (DataDome/SmartCaptcha) добираем её через
# Camoufox ОТДЕЛЬНЫМ процессом: изоляция зависимостей (свой playwright 1.51) и RAM
# (освобождается по выходу). Управление через ENV: CAMOUFOX_FALLBACK=0 — выключить.
CAMOUFOX_PY = os.environ.get("CAMOUFOX_PY", "/opt/camoufox-test/.venv/bin/python")
CAMOUFOX_SCRIPT = str(Path(__file__).parent / "camoufox_scrape.py")
CAMOUFOX_FALLBACK = os.environ.get("CAMOUFOX_FALLBACK", "1") != "0"
CAMOUFOX_MAX_PAGES = int(os.environ.get("CAMOUFOX_MAX_PAGES", "2"))


def camoufox_fallback(platform: str, queries: list, run_log) -> tuple[list[dict], dict]:
    """Добор площадки запасным движком (Firefox) отдельным процессом.

    Возвращает (items, stats). items — в том же формате, что scrape_one_platform
    (id/title/price/url/platform/query), готовы к dedup+upsert. Best-effort: любая
    ошибка подавляется, возвращаем что есть (основной путь уже отработал)."""
    import subprocess
    if not os.path.exists(CAMOUFOX_PY) or not os.path.exists(CAMOUFOX_SCRIPT):
        run_log(f"  [{platform}] camoufox-fallback недоступен (нет venv/скрипта)")
        return [], {}
    cmd = [CAMOUFOX_PY, CAMOUFOX_SCRIPT, platform, *queries, "--max-pages", str(CAMOUFOX_MAX_PAGES)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        run_log(f"  [{platform}] camoufox-fallback упал: {str(e)[:150]}")
        return [], {}
    if r.returncode != 0 or not (r.stdout or "").strip():
        run_log(f"  [{platform}] camoufox-fallback без результата (rc={r.returncode}): {(r.stderr or '')[:150]}")
        return [], {}
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        run_log(f"  [{platform}] camoufox-fallback: JSON не распарсен: {str(e)[:120]}")
        return [], {}
    return out.get("items", []), out.get("stats", {})


# === Прокси-ступень каскада: CloakBrowser через РФ-прокси (свежий IP) ===============
# Тот же venv, что у основного парсера (cloakbrowser), запускается ОТДЕЛЬНЫМ процессом
# (RAM освобождается по выходу, как camoufox). Идёт ДО Camoufox: другой РФ-IP часто
# проходит там, где прямой флагнут DataDome по velocity.
CLOAK_PROXY_PY = os.environ.get("CLOAK_PROXY_PY", sys.executable)
CLOAK_PROXY_SCRIPT = str(Path(__file__).parent / "cloak_proxy_scrape.py")


def _tunnel_alive(proxy: str) -> bool:
    """Быстрый чек живости SOCKS-туннеля: слушает ли локальный порт. socks5://host:port."""
    import socket
    try:
        hp = proxy.split("://", 1)[-1]
        host, port = hp.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=4):
            return True
    except Exception:
        return False


def cloak_proxy_fallback(platform: str, queries: list, proxy: str, run_log) -> tuple[list[dict], dict, bool]:
    """Повторный проход CloakBrowser через РФ-прокси (свежий IP) отдельным процессом.
    Возвращает (items, stats, blocked). Best-effort: при недоступности туннеля/ошибке
    возвращает ([], {}, False) — каскад идёт дальше к Camoufox."""
    import subprocess
    if not os.path.exists(CLOAK_PROXY_SCRIPT):
        run_log(f"  [{platform}] cloak-proxy недоступен (нет скрипта)")
        return [], {}, False
    if not _tunnel_alive(proxy):
        run_log(f"  [{platform}] cloak-proxy: туннель {proxy} мёртв — пропускаю ступень")
        return [], {}, False
    cmd = [CLOAK_PROXY_PY, CLOAK_PROXY_SCRIPT, platform, *queries,
           "--proxy", proxy, "--max-pages", str(CLOAK_PROXY_MAX_PAGES)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        run_log(f"  [{platform}] cloak-proxy упал: {str(e)[:150]}")
        return [], {}, False
    if r.returncode != 0 or not (r.stdout or "").strip():
        run_log(f"  [{platform}] cloak-proxy без результата (rc={r.returncode}): {(r.stderr or '')[:150]}")
        return [], {}, False
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        run_log(f"  [{platform}] cloak-proxy: JSON не распарсен: {str(e)[:120]}")
        return [], {}, False
    return out.get("items", []), out.get("stats", {}), bool(out.get("blocked", False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platforms", default=",".join(PLATFORMS), help="ozon,wildberries,yandex,avito")
    ap.add_argument("--trigger", default="cron", choices=["cron", "manual", "watcher", "dashboard"])
    ap.add_argument("--only-query", default=None,
                    help="Парсить только этот запрос (для быстрого прогона нового запроса с дашборда)")
    ap.add_argument("--no-notify", action="store_true",
                    help="Не слать n8n/Telegram-уведомление (для ночного cron: один отчёт шлёт оркестратор в 04:00)")
    args = ap.parse_args()
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    # Порядок: сначала надёжные (ozon, wildberries), потом склонные к зависанию
    # (avito SPA-redirect, yandex DataDome) — yandex ПОСЛЕДНИМ. Так зависание одной
    # площадки не лишает данных остальные (каждая апсертится сразу после скрейпа).
    SCRAPE_ORDER = {"ozon": 0, "wildberries": 1, "avito": 2, "yandex": 3}
    platforms.sort(key=lambda p: SCRAPE_ORDER.get(p, 9))

    # QUERIES: либо один запрос (--only-query), либо активные из monitor_queries.
    global QUERIES
    if args.only_query:
        QUERIES = [args.only_query]
        log("режим single-query", query=args.only_query)
    else:
        QUERIES = load_queries()
    log("queries загружены", count=len(QUERIES))

    log("=== run start ===", platforms=",".join(platforms), proxy=SCRAPER_PROXY or "direct")
    # Ретраим создание parser_runs: при кратком сбое Supabase в 00:00 прогон иначе
    # падал бы без единой записи (watchdog/healthcheck/оркестратор слепы). Если все
    # попытки провалились — выходим; отсутствие данных за сегодня поймает оркестратор
    # (аномалия zero_written) в 04:00.
    run_id = None
    for _att in range(3):
        try:
            run_id = insert_parser_run(args.trigger)
            break
        except Exception as e:
            log("insert_parser_run FAILED — Supabase недоступен?", attempt=_att + 1, err=str(e)[:200])
            if _att < 2:
                time.sleep(10)
    if run_id is None:
        log("=== run aborted: parser_runs не создан за 3 попытки, прогон не выполнен ===")
        sys.exit(1)
    log("run id", id=run_id, pid=os.getpid())

    # Heartbeat-поток: каждые 30 сек обновляет parser_runs.last_heartbeat.
    # Watchdog на VDSina убьёт прогон, если heartbeat молчит >5 мин.
    heartbeat_stop = start_heartbeat_thread(run_id, interval=30)

    totals: dict = {}
    all_errors: list[dict] = []
    daily_rows: list[dict] = []

    launch_kwargs = {"headless": True, "humanize": True}
    if SCRAPER_PROXY:
        launch_kwargs["proxy"] = SCRAPER_PROXY
    browser = launch(**launch_kwargs)
    try:
        page = browser.new_page()
        # Защита от вечного зависания: жёсткие таймауты на всех уровнях.
        # Cloakbrowser/Playwright по умолчанию ждёт 30 сек, но page.evaluate
        # вообще без timeout — поэтому страница с DataDome-challenge может
        # висеть бесконечно. Эти setter'ы делают timeout явным.
        try:
            page.set_default_navigation_timeout(45000)
            page.set_default_timeout(30000)
        except Exception:
            pass
        for plat in platforms:
            try:
                items, errors = scrape_one_platform(page, plat, log)
                # Camoufox-fallback: если CloakBrowser упёрся в антибот (метки BLOCKED
                # в errors) — добираем эту площадку запасным движком (Firefox) и мёржим.
                # Статистика движков копится в totals → за N прогонов видно, как часто
                # CloakBrowser блокируется и сколько из этого спасает Camoufox.
                blocked_cloak = sum(1 for e in errors if "BLOCKED" in (e.get("message") or ""))
                direct_count = len(items)        # карточки прямого прохода (до fallback)
                recovered_proxy = 0
                recovered_cam = 0
                # Ступень 1 каскада: при блоке прямого IP — проход CloakBrowser через
                # РФ-прокси (свежий IP). Только если для площадки задан прокси и ступень вкл.
                plat_proxy = PLATFORM_PROXY.get(plat)
                proxy_blocked = False
                if blocked_cloak and CLOAK_PROXY_FALLBACK and plat_proxy:
                    log(f"  [{plat}] CloakBrowser заблокирован ({blocked_cloak}) → проход через прокси {plat_proxy}")
                    px_items, px_stats, proxy_blocked = cloak_proxy_fallback(plat, QUERIES, plat_proxy, log)
                    if px_items:
                        items.extend(px_items)
                        recovered_proxy = len(px_items)
                        log(f"  [{plat}] прокси-проход добрал {recovered_proxy} карточек", **(px_stats or {}))
                # Ступень 2 (финал): Camoufox — если прямой блок и прокси не закрыл вопрос
                # (прокси не задан / тоже заблокирован / ничего не добрал).
                if blocked_cloak and CAMOUFOX_FALLBACK and (not plat_proxy or proxy_blocked or recovered_proxy == 0):
                    log(f"  [{plat}] CloakBrowser заблокирован ({blocked_cloak}) → Camoufox-fallback")
                    cf_items, cf_stats = camoufox_fallback(plat, QUERIES, log)
                    if cf_items:
                        items.extend(cf_items)
                        recovered_cam = len(cf_items)
                        log(f"  [{plat}] Camoufox добрал {recovered_cam} карточек", **(cf_stats or {}))
                # Метка движков, реально давших карточки: cloak / cloak+proxy / proxy+camoufox …
                _parts = (["cloak"] if direct_count else []) + \
                         (["proxy"] if recovered_proxy else []) + \
                         (["camoufox"] if recovered_cam else [])
                engine = "+".join(_parts) or "cloak"
                # Guard (UI): запрос мог быть удалён с дашборда за время прогона
                # (remove_queries отменяет parse_queue + удаляет monitor_queries). Тогда
                # карточки удалённого запроса НЕ пишем — иначе они осиротеют в БД.
                if args.only_query and not is_query_active(args.only_query):
                    log(f"  [{plat}] запрос '{args.only_query}' удалён за время прогона — карточки не пишем, прерываю прогон")
                    break
                items_d = dedup(items)
                ok = upsert_listings(items_d)
                totals[plat] = {"found": len(items), "unique": len(items_d), "upserted": ok,
                                "engine": engine, "blocked_cloak": blocked_cloak,
                                "recovered_proxy": recovered_proxy,
                                "recovered_camoufox": recovered_cam}
                ds = platform_daily_stat(plat, items_d)
                daily_rows.append(ds)
                # daily_stats пишем СРАЗУ по площадке (а не в конце прогона) — иначе
                # зависание на последней площадке теряет дневной срез успевших.
                if not args.only_query:
                    try:
                        upsert_daily_stats([ds])
                    except Exception as e:
                        log("daily_stats (инкр.) fail", err=str(e)[:150])
                # Camoufox восстановил площадку → снимаем отметки BLOCKED, чтобы
                # успешно спасённый прогон не помечался partial из-за погашенной блокировки.
                if recovered_proxy or recovered_cam:
                    errors = [e for e in errors if "BLOCKED" not in (e.get("message") or "")]
                all_errors.extend(errors)
                log(f"  [{plat}] done", **totals[plat])
            except Exception as e:
                all_errors.append({"platform": plat, "stage": "scrape_platform", "message": str(e)[:300]})
                log(f"  [{plat}] FAIL", err=str(e)[:200])
    finally:
        browser.close()

    has_data = sum(t.get("upserted", 0) for t in totals.values()) > 0
    status = "ok" if has_data and not all_errors else ("partial" if has_data else "failed")

    # Retention: только если прогон успешен (ok/partial с данными).
    # Если прогон полностью провалился — не удаляем, иначе можем потерять
    # данные при сетевой проблеме (хочется иметь хоть что-то на дашборде).
    if has_data:
        deleted = cleanup_old_listings(retention_days=14)
        log("retention: удалено карточек старше 14 дней", deleted=deleted)
        totals["_retention"] = {"listings_deleted": deleted}
        orphan_deleted = cleanup_orphan_listings_rpc(days=7)
        if orphan_deleted and orphan_deleted > 0:
            log("retention: удалено осиротевших карточек (удалённые запросы >7д)", deleted=orphan_deleted)
        totals["_retention"]["orphan_deleted"] = orphan_deleted
        runs_deleted = cleanup_old_parser_runs(retention_days=90)
        if runs_deleted > 0:
            log("retention: удалено parser_runs старше 90 дней", deleted=runs_deleted)

    # daily_stats теперь пишется инкрементально внутри цикла (по каждой площадке
    # сразу) — чтобы зависание на последней площадке не теряло дневной срез
    # успевших. Здесь только логируем итог.
    if has_data and not args.only_query:
        log("daily_stats записан инкрементально", platforms=len(daily_rows))

    # Отметим запросы как обработанные (для дашборда — когда последний раз парсился)
    if has_data:
        for q in QUERIES:
            mark_query_parsed(q)

    # Финализация с одним ретраем. heartbeat НЕ останавливаем до финализации —
    # держим живым во время ретраев (иначе при сетевом сбое прогон завис бы в
    # 'running' с замороженным heartbeat → ложный hung от watchdog). Останавливаем
    # в finally. Если финализация так и не прошла — данные в listings/daily_stats
    # уже записаны инкрементально, watchdog пометит прогон hung.
    finalized = False
    try:
        for _att in range(2):
            try:
                finalize_parser_run(run_id, totals, all_errors, status)
                finalized = True
                break
            except Exception as e:
                log("finalize_parser_run FAILED", attempt=_att + 1, err=str(e)[:200])
                if _att == 0:
                    time.sleep(5)
    finally:
        heartbeat_stop.set()  # стоп heartbeat-потока
    if not finalized:
        log("=== finalize не удался — прогон останется 'running', watchdog пометит hung ===")
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(1)
    log("=== run end ===", status=status, totals=totals, errors=len(all_errors))
    if not args.no_notify:
        n8n_notify({"run_id": run_id, "status": status, "totals": totals, "errors_count": len(all_errors)})
    else:
        log("n8n notify пропущен (--no-notify): отчёт пришлёт оркестратор")
    # ВАЖНО: os._exit, а не return/sys.exit. cloakbrowser/Playwright оставляет
    # non-daemon потоки и дочерний chromium, из-за которых процесс зависал после
    # завершения работы (run end в логе есть, а процесс жил ещё часами и держал
    # cloakbrowser-ресурс — watchdog не ловил, т.к. run уже finalized=ok).
    # os._exit рвёт процесс немедленно, минуя ожидание потоков и atexit.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0 if status != "failed" else 1)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Детектор аномалий данных для anti-ms-mp.

Сверяет «сколько парсер НАШЁЛ» (parser_runs.totals последнего прогона) с тем,
«сколько РЕАЛЬНО видно на дашборде» (карточки с ценой = контрафакт за сегодня).
Ловит класс ошибок «нашли 400 — показываем 0» (как было с Яндексом: сменилась
вёрстка → перестала сниматься цена → карточки без цены не считаются контрафактом).

Только чтение (anon key). Печатает JSON в stdout: {healthy, platforms, anomalies, run}.
Запускается оркестратором (orchestrator.sh) в 04:00 МСК на Vultr.

ENV: SUPABASE_URL, SUPABASE_ANON_KEY
"""
import json, os, sys, urllib.request, urllib.parse, datetime as dt

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
ANON = os.environ.get("SUPABASE_ANON_KEY", "")
PLATFORMS = ["ozon", "wildberries", "yandex", "avito"]
PL_RU = {"ozon": "Ozon", "wildberries": "Wildberries", "yandex": "Яндекс.Маркет", "avito": "Avito"}

# Пороги аномалий
MIN_FOUND = 40          # площадка считается «активной», если нашли столько карточек
MIN_COVERAGE = 0.25     # доля карточек с ценой ниже этого = вероятно сломан съём цены


def _get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get_all(path_with_q):
    """Постраничная выгрузка через Range (PostgREST отдаёт максимум 1000)."""
    out, frm = [], 0
    while True:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{path_with_q}",
            headers={"apikey": ANON, "Authorization": f"Bearer {ANON}",
                     "Range-Unit": "items", "Range": f"{frm}-{frm+999}"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read().decode())
        out += batch
        if len(batch) < 1000:
            break
        frm += 1000
    return out


def main():
    if not SUPABASE_URL or not ANON:
        print(json.dumps({"error": "SUPABASE_URL/SUPABASE_ANON_KEY не заданы"}))
        return 2

    # Дата «сегодня» по Москве (last_seen парсер пишет в МСК-дате)
    msk_today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).date().isoformat()

    # Последний завершённый прогон. Предпочитаем ПОЛНЫЙ (≥3 площадок в totals),
    # чтобы found был осмыслен — ручной single-query прогон не должен сбивать сводку.
    runs = _get("parser_runs?select=id,status,trigger,started_at,finished_at,totals"
                "&order=finished_at.desc.nullslast&limit=15")
    def _full(r):
        t = r.get("totals") or {}
        return sum(1 for p in PLATFORMS if p in t) >= 3
    last = next((r for r in runs if r.get("status") in ("ok", "partial") and _full(r)), None)
    if last is None:
        last = next((r for r in runs if r.get("status") in ("ok", "partial") and r.get("totals")), None)
    run_totals = (last or {}).get("totals") or {}

    # Самый свежий прогон (любой статус, по id) — чтобы отличить «прогон завис/упал
    # и не дошёл до площадки» от «экстрактор сломан».
    latest = max(runs, key=lambda r: r.get("id") or 0) if runs else None
    latest_status = (latest or {}).get("status")
    run_incomplete = latest_status in ("hung", "failed", "running")

    # Карточки за сегодня (только нужные поля)
    rows = _get_all(f"listings?select=pl,price,op&last_seen=eq.{msk_today}")

    platforms, anomalies = [], []
    for p in PLATFORMS:
        prs = [x for x in rows if x.get("pl") == p]
        seen = len(prs)
        withp = sum(1 for x in prs if (x.get("price") or 0) > 0)
        cf = sum(1 for x in prs if (x.get("price") or 0) > 0
                 and (x.get("op") or 0) > 0 and x["price"] < 0.5 * x["op"])
        cov = round(withp / seen, 3) if seen else 0.0
        found = int((run_totals.get(p) or {}).get("upserted")
                    or (run_totals.get(p) or {}).get("unique") or 0)
        platforms.append({"pl": p, "found": found, "seen": seen,
                          "with_price": withp, "counterfeit": cf, "coverage": cov})

        # Аномалия 0: площадка без данных за сегодня + последний прогон не завершился
        # штатно → причина в зависании/сбое прогона, НЕ в экстракторе (не чинить код,
        # нужен перезапуск парсера).
        if seen == 0 and run_incomplete:
            anomalies.append({
                "pl": p, "kind": "run_incomplete",
                "detail": (f"{PL_RU[p]}: нет данных за сегодня — ночной прогон "
                           f"#{(latest or {}).get('id')} завершился со статусом "
                           f"'{latest_status}' и не дошёл до этой площадки. "
                           f"Экстрактор в порядке — нужен перезапуск парсера.")
            })
        # Аномалия 0b: прогон завершился ШТАТНО, но за сегодня по площадке 0 карточек.
        # Мёртвая зона прежней логики: не run_incomplete, found мог быть 0 → ни одно
        # из условий ниже не срабатывало, и площадка тихо пропадала с дашборда.
        elif seen == 0 and not run_incomplete:
            anomalies.append({
                "pl": p, "kind": "zero_written",
                "detail": (f"{PL_RU[p]}: последний прогон завершился штатно, но карточек "
                           f"за сегодня нет (0). Возможен тихий сбой записи (upsert) либо "
                           f"площадка не попала в прогон — пропала с дашборда без явной ошибки.")
            })
        # Аномалия 1: нашли много, но контрафакт не виден вообще
        elif found >= MIN_FOUND and cf == 0:
            anomalies.append({
                "pl": p, "kind": "found_but_invisible",
                "detail": (f"{PL_RU[p]}: парсер нашёл {found} карточек, но на дашборде "
                           f"контрафакта 0 (с ценой {withp} из {seen}). Вероятно сломан "
                           f"съём цены или распознавание после смены вёрстки маркетплейса.")
            })
        # Аномалия 2: низкое покрытие ценой (частично сломан съём)
        elif seen >= MIN_FOUND and cov < MIN_COVERAGE:
            anomalies.append({
                "pl": p, "kind": "low_price_coverage",
                "detail": (f"{PL_RU[p]}: цена снята лишь у {withp} из {seen} карточек "
                           f"({int(cov*100)}%). Похоже, селектор цены частично перестал работать.")
            })

    result = {
        "msk_date": msk_today,
        "healthy": len(anomalies) == 0,
        "run": {"id": (last or {}).get("id"), "status": (last or {}).get("status"),
                "trigger": (last or {}).get("trigger"),
                "finished_at": (last or {}).get("finished_at"),
                "latest_id": (latest or {}).get("id"), "latest_status": latest_status},
        "platforms": platforms,
        "anomalies": anomalies,
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

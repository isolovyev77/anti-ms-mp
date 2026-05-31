#!/usr/bin/env python3
"""anti-ms-mp dead-man's switch (#18) — независимый сторож на Oracle (clawbot).

Независим от Vultr и VDSina. Раз в час (cron) проверяет в Supabase:
  - system_heartbeats[orchestrator].beat_at — оркестратор (Vultr) отметился < ORCH_MAX_H ч
  - parser_runs последний finished_at — парсер (VDSina) завершал прогон < PARSER_MAX_H ч
Если что-то протухло — шлёт алерт НАПРЯМУЮ в Telegram Bot API (api.telegram.org),
минуя n8n на Vultr → алерт доходит даже если Vultr полностью мёртв.

Дедуп: не чаще раза в RE_ALERT_H ч на каждый вид простоя (state.json).
Конфиг: ./.env (рядом). Запуск из cron: `python3 watch.py`. Тест канала: `--test`.

ENV (.env, права 600):
  IS_N8N_TELEGRAM_BOT_TOKEN   токен бота (того же, что шлёт отчёты оркестратора)
  TG_CHAT_ID                  чат получателя
  SUPABASE_URL, SUPABASE_ANON_KEY
"""
import json, os, sys, time, urllib.request, urllib.parse, datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV, STATE, LOG = HERE / ".env", HERE / "state.json", HERE / "watch.log"
ORCH_MAX_H = 25       # оркестратор раз в сутки (04:00 МСК) → >25ч = пропуск
PARSER_MAX_H = 36     # парсер раз в сутки + запас на авто-перезапуск
RE_ALERT_H = 6        # не спамить чаще раза в 6ч на один и тот же простой


def load_env():
    e = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                e[k.strip()] = v.strip()
    return e


E = load_env()
SUPA = E.get("SUPABASE_URL", "").rstrip("/")
ANON = E.get("SUPABASE_ANON_KEY", "")
TOKEN = E.get("IS_N8N_TELEGRAM_BOT_TOKEN") or E.get("TELEGRAM_BOT_TOKEN", "")
CHAT = E.get("TG_CHAT_ID", "")


def log(msg):
    line = f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)


def sget(path):
    req = urllib.request.Request(
        f"{SUPA}/rest/v1/{path}",
        headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def tg_send(text):
    data = urllib.parse.urlencode(
        {"chat_id": CHAT, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def age_hours(iso):
    if not iso:
        return None
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s):
    try:
        STATE.write_text(json.dumps(s))
    except Exception:
        pass


def main():
    if not (SUPA and ANON and TOKEN and CHAT):
        log("config missing (.env)")
        return 1

    if "--test" in sys.argv:
        r = tg_send("✅ [ТЕСТ] anti-ms-mp: независимый сторож на Oracle подключён. "
                    "Разовая проверка канала — реальные алерты придут только если "
                    "оркестратор (Vultr) или парсер (VDSina) замолчат.")
        log(f"test sent ok={r.get('ok')}")
        return 0 if r.get("ok") else 2

    problems = []
    try:
        hb = sget("system_heartbeats?component=eq.orchestrator&select=beat_at")
        a = age_hours(hb[0]["beat_at"]) if hb else None
        if a is None:
            problems.append(("orchestrator", "нет ни одной отметки живости оркестратора (Vultr)"))
        elif a > ORCH_MAX_H:
            problems.append(("orchestrator",
                             f"оркестратор (Vultr) молчит {a:.0f}ч (порог {ORCH_MAX_H}ч) — "
                             f"ночной прогон/cron не отработал?"))
    except Exception as e:
        log(f"orch check err: {str(e)[:200]}")

    try:
        runs = sget("parser_runs?select=finished_at,status&order=finished_at.desc.nullslast&limit=1")
        fin = runs[0].get("finished_at") if runs else None
        a = age_hours(fin)
        if a is not None and a > PARSER_MAX_H:
            problems.append(("parser",
                             f"парсер (VDSina) не завершал прогон {a:.0f}ч (порог {PARSER_MAX_H}ч)"))
    except Exception as e:
        log(f"parser check err: {str(e)[:200]}")

    state, fired = load_state(), []
    active = [p[0] for p in problems]
    for key, text in problems:
        if (time.time() - state.get(key, 0)) > RE_ALERT_H * 3600:
            try:
                tg_send(f"🔴 anti-ms-mp watchdog (Oracle): {text}\n"
                        f"Проверь VDSina/Vultr/cron. Независимый сторож — алерт идёт мимо n8n.")
                state[key] = time.time()
                fired.append(key)
            except Exception as e:
                log(f"alert send err: {str(e)[:200]}")
    for key in list(state.keys()):       # сброс отметок по разрешившимся простоям
        if key not in active:
            state.pop(key, None)
    save_state(state)
    log(f"checked: problems={active} fired={fired}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

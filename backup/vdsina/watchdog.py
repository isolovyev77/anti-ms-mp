#!/usr/bin/env python3
"""Watchdog для parser_runner: убивает зависшие прогоны.

Логика:
1. Запрашиваем у Supabase parser_runs со status='running' и hostname=$(hostname)
2. Если last_heartbeat старше STALE_AFTER_SEC (по умолчанию 5 мин):
   - kill -9 PID
   - PATCH parser_runs: status='hung', finished_at=NOW(), notes=<краткое описание>
   - POST в n8n webhook с алертом для Telegram

Запускается по cron на VDSina:
  */5 * * * * cd /opt/anti-ms-mp && .venv/bin/python watchdog.py >> /var/log/anti-ms-watchdog.log 2>&1

ENV: тот же .env что у parser_runner.py.
"""
import datetime as dt
import json
import os
import signal
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Загрузка .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")
N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK_PARSER_DONE", "")
HOSTNAME = socket.gethostname()
STALE_AFTER_SEC = int(os.environ.get("WATCHDOG_STALE_AFTER_SEC", "300"))  # 5 мин по умолчанию


def log(msg: str, **kw) -> None:
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    extra = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[{ts}] [watchdog] {msg} {extra}".rstrip(), flush=True)


def supabase_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def supabase_patch(path: str, payload: dict) -> None:
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


def alert_telegram(run_id: int, pid: int, stale_seconds: int, started_at: str) -> None:
    if not N8N_WEBHOOK:
        log("no N8N_WEBHOOK configured, skip alert")
        return
    # text_override: человеческий текст вместо собранного n8n «Карточек залито: 0».
    # При зависании итог прогона (totals) не финализируется — ноль означает «итог не
    # подведён», а НЕ «ничего не собрано» (по ходу карточки писались). Чтобы ночное
    # сообщение не пугало нулём, явно поясняем и переадресуем к утреннему отчёту 04:00,
    # где будут актуальные цифры. n8n на этом webhook уважает text_override (как у оркестратора).
    text = (
        f"\U0001F4E6 Парсер anti-ms-mp\n"
        f"Прогон #{run_id} не завершился — завис и был остановлен сторожем "
        f"(после {stale_seconds//60} мин без отклика).\n"
        f"Это не значит, что данные потеряны: карточки писались по ходу, "
        f"но итог прогона не подведён.\n"
        f"Актуальные цифры и, при необходимости, перезапуск — в утреннем отчёте в 04:00."
    )
    payload = {
        "run_id": run_id,
        "status": "hung",
        "totals": {},
        "errors_count": 1,
        "alert": True,
        "alert_kind": "watchdog_killed",
        "details": f"PID {pid} убит после {stale_seconds//60} мин без heartbeat (started {started_at})",
        "text_override": text,
    }
    req = urllib.request.Request(
        N8N_WEBHOOK,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log("alert sent", run_id=run_id)
    except Exception as e:
        log("alert FAILED", err=str(e)[:200])


def is_our_parser(pid: int) -> bool:
    """PID всё ещё наш parser_runner? Защита от убийства ПЕРЕИСПОЛЬЗОВАННОГО PID:
    если прогон умер без финализации, а ОС отдала его PID чужому процессу, слепой
    SIGKILL прилетел бы постороннему. Читаем /proc/<pid>/cmdline (VDSina=Linux),
    матч по подстроке parser_runner.py — как ps-скан возрастного капа ниже."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        return "parser_runner.py" in cmd
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def kill_if_ours(pid: int) -> tuple:
    """SIGKILL процесс ТОЛЬКО если он всё ещё наш parser_runner. (killed, note).
    Окно cmdline→kill миллисекундное, вероятность реюза PID в нём пренебрежима —
    но несравнимо безопаснее прежнего «убиваем по PID из БД вслепую»."""
    if not is_our_parser(pid):
        return False, "не убит: pid не наш parser_runner (переиспользован/завершён)"
    try:
        os.kill(pid, signal.SIGKILL)
        return True, "killed"
    except (ProcessLookupError, PermissionError) as e:
        return False, f"не убит: {type(e).__name__}"


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("config missing SUPABASE_URL/KEY")
        return 1

    # Берём все running прогоны: на этом хосте ИЛИ с hostname=null (zombie до v2).
    # PostgREST: or=(hostname.eq.X,hostname.is.null)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        runs = supabase_get(
            f"/rest/v1/parser_runs?status=eq.running"
            f"&or=(hostname.eq.{HOSTNAME},hostname.is.null)"
            f"&select=id,started_at,last_heartbeat,pid,hostname"
        )
        log("scan", total_running=len(runs), host=HOSTNAME)
    except Exception as e:
        # Supabase недоступен — не роняем процесс, иначе страховка №2 (ps-скан по возрасту) не выполнится
        log("supabase_get FAILED — пропускаю heartbeat-проверку, только ps-fallback", err=str(e)[:200])
        runs = []

    killed = 0
    for r in runs:
        rid = r["id"]
        pid = r.get("pid")
        hb = r.get("last_heartbeat") or r.get("started_at")
        if not hb:
            continue
        # Парсим ISO timestamp с timezone
        try:
            hb_dt = dt.datetime.fromisoformat(hb.replace("Z", "+00:00"))
        except Exception:
            log("bad heartbeat format", run_id=rid, hb=hb)
            continue
        stale = int((now - hb_dt).total_seconds())
        if stale < STALE_AFTER_SEC:
            log("OK", run_id=rid, pid=pid, stale_sec=stale)
            continue

        # Прогон протух — убиваем (только если PID всё ещё наш parser_runner)
        killed_ok, kill_note = kill_if_ours(pid) if pid else (False, "нет pid")
        supabase_patch(
            f"/rest/v1/parser_runs?id=eq.{rid}",
            {
                "status": "hung",
                "finished_at": now.isoformat(),
                "notes": f"watchdog: stale {stale}s, pid {pid}, {kill_note}",
            },
        )
        alert_telegram(rid, pid or 0, stale, r.get("started_at", ""))
        log("HUNG", run_id=rid, pid=pid, stale_sec=stale, killed=killed_ok, note=kill_note)
        killed += 1

    # Есть ли прямо сейчас ЗДОРОВЫЙ активный прогон (running + свежий heartbeat)?
    # Если да — возрастной кап не должен его трогать: им управляет heartbeat-детектор
    # (5 мин). Иначе здоровый, но долгий из-за fallback'ов (Camoufox/прокси-каскад)
    # прогон срубается на 45-й минуте посреди последней площадки (кейс #52, 11.06).
    has_healthy_run = False
    for r in runs:
        hb = r.get("last_heartbeat") or r.get("started_at")
        if not hb:
            continue
        try:
            if int((now - dt.datetime.fromisoformat(hb.replace("Z", "+00:00"))).total_seconds()) < STALE_AFTER_SEC:
                has_healthy_run = True
                break
        except Exception:
            pass

    # Страховка №2: убиваем процессы parser_runner старше MAX_PROC_AGE_SEC,
    # даже если их parser_runs уже finalized (status=ok). Был кейс: парсер дошёл
    # до "run end", но cloakbrowser оставил non-daemon потоки/chromium, и процесс
    # висел 17 часов, держа ресурс. Такой зомби не виден через parser_runs.
    MAX_PROC_AGE_SEC = int(os.environ.get("WATCHDOG_MAX_PROC_AGE_SEC", "2700"))  # 45 мин — кап для ЗОМБИ/осиротевших (нет свежего heartbeat); живой прогон им не трогается
    HARD_CEILING_SEC = int(os.environ.get("WATCHDOG_HARD_CEILING_SEC", "14400"))  # 4 ч — финальный потолок против runaway/livelock (heartbeat жив, но прогон не кончается)
    # Динамический кап: пока есть здоровый активный прогон — поднимаем порог до
    # аварийного потолка (heartbeat сам убьёт реальный фриз за 5 мин); зомби без
    # свежего heartbeat по-прежнему режутся за 45 мин.
    effective_cap = HARD_CEILING_SEC if has_healthy_run else MAX_PROC_AGE_SEC
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-eo", "pid,etimes,comm,args"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            if "parser_runner.py" not in line:
                continue
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid_i = int(parts[0]); age = int(parts[1])
            except ValueError:
                continue
            if pid_i == os.getpid():
                continue
            if age > effective_cap:
                try:
                    os.kill(pid_i, signal.SIGKILL)
                    log("KILLED stale process (by age)", pid=pid_i, age_sec=age, cap=effective_cap)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
            elif age > MAX_PROC_AGE_SEC and has_healthy_run:
                # старше 45 мин, но прогон жив (свежий heartbeat) — НЕ трогаем
                log("skip age-kill: healthy active run", pid=pid_i, age_sec=age, cap=effective_cap)
    except Exception as e:
        log("proc-age scan failed", err=str(e)[:150])

    log("done", killed=killed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

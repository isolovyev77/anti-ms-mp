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


def kill_if_alive(pid: int) -> bool:
    """SIGKILL процесс. Возвращает True если процесс жив был."""
    try:
        os.kill(pid, signal.SIGKILL)  # единый kill: ProcessLookupError покрывает несуществующий PID без TOCTOU-окна между проверкой и убийством
        return True
    except (ProcessLookupError, PermissionError):
        return False


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

        # Прогон протух — убиваем
        was_alive = kill_if_alive(pid) if pid else False
        supabase_patch(
            f"/rest/v1/parser_runs?id=eq.{rid}",
            {
                "status": "hung",
                "finished_at": now.isoformat(),
                "notes": f"watchdog kill: stale {stale}s, pid {pid}, was_alive={was_alive}",
            },
        )
        alert_telegram(rid, pid or 0, stale, r.get("started_at", ""))
        log("KILLED", run_id=rid, pid=pid, stale_sec=stale, was_alive=was_alive)
        killed += 1

    # Страховка №2: убиваем процессы parser_runner старше MAX_PROC_AGE_SEC,
    # даже если их parser_runs уже finalized (status=ok). Был кейс: парсер дошёл
    # до "run end", но cloakbrowser оставил non-daemon потоки/chromium, и процесс
    # висел 17 часов, держа ресурс. Такой зомби не виден через parser_runs.
    MAX_PROC_AGE_SEC = int(os.environ.get("WATCHDOG_MAX_PROC_AGE_SEC", "2700"))  # 45 мин — запас (здоровый полный прогон ~25-30 мин); основной ловец зависаний — застывший heartbeat (5 мин)
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
            if age > MAX_PROC_AGE_SEC:
                try:
                    os.kill(pid_i, signal.SIGKILL)
                    log("KILLED stale process (by age)", pid=pid_i, age_sec=age)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception as e:
        log("proc-age scan failed", err=str(e)[:150])

    log("done", killed=killed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

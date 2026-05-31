#!/usr/bin/env python3
"""Poller очереди parse_queue: запускает парсер по запросам с дашборда.

Логика (cron каждую минуту):
1. Если parser_runner уже работает — выходим (нельзя два cloakbrowser параллельно).
2. Берём самый старый pending из parse_queue.
3. Ставим ему status=running.
4. Запускаем parser_runner:
   - query задан → --only-query "<query>" (быстрый прогон одного запроса)
   - query NULL   → полный прогон всех active queries (--trigger dashboard)
5. По завершении: status=done (или failed), processed_at=now.

Запуск по cron на VDSina:
  * * * * * cd /opt/anti-ms-mp && .venv/bin/python queue_poller.py >> /var/log/anti-ms-queue.log 2>&1

ENV: тот же .env, что у parser_runner (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY).
"""
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
env_path = BASE / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PYTHON = str(BASE / ".venv" / "bin" / "python")
PARSER = str(BASE / "parser_runner.py")


def log(msg, **kw):
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    extra = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[{ts}] [queue] {msg} {extra}".rstrip(), flush=True)


def sb_get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
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


def sb_patch_claim(jid) -> bool:
    """Атомарный захват задачи: pending→running по id ТОЛЬКО если ещё pending.
    Возвращает True, если строка досталась нам (PostgREST вернул её в ответе).
    Закрывает TOCTOU между pgrep-проверкой и запуском: два поллера не запустят
    два параллельных cloakbrowser, даже если оба прошли parser_running()."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/parse_queue?id=eq.{jid}&status=eq.pending",
        data=json.dumps({"status": "running"}).encode(),
        method="PATCH",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
    return bool(json.loads(body) if body else [])


def parser_running() -> bool:
    """Проверяем, не запущен ли уже parser_runner (любой процесс)."""
    try:
        out = subprocess.run(["pgrep", "-f", "parser_runner.py"],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and out.stdout.strip() != ""
    except Exception:
        return False


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("config missing")
        return 1

    # Dead-letter recovery: задачи, застрявшие в 'running' дольше 60 мин (поллер
    # был убит до записи статуса — SIGKILL/OOM/рестарт), помечаем failed, чтобы
    # дашборд не «крутился» вечно и очередь не засорялась. Порог 60 мин > макс.
    # прогона (~40 мин), поэтому ещё живой прогон не трогаем.
    try:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=60)).isoformat()
        for s in sb_get(f"/rest/v1/parse_queue?status=eq.running&requested_at=lt.{cutoff}&select=id"):
            sb_patch(f"/rest/v1/parse_queue?id=eq.{s['id']}", {
                "status": "failed",
                "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "note": "dead-letter: завис в running >60 мин (поллер прерван до записи статуса)",
            })
            log("dead-letter: сброс зависшей задачи", queue_id=s["id"])
    except Exception as e:
        log("dead-letter recovery fail", err=str(e)[:150])

    pending = sb_get("/rest/v1/parse_queue?status=eq.pending&order=requested_at.asc&limit=1")
    if not pending:
        return 0  # тихо: очередь пуста (cron каждую минуту, не засоряем лог)

    job = pending[0]
    jid, query = job["id"], job.get("query")

    # Не запускаем второй парсер параллельно (cloakbrowser не делится между процессами)
    if parser_running():
        log("parser уже работает, откладываю", queue_id=jid)
        return 0

    # Атомарный захват: если другой экземпляр поллера уже взял задачу (пустой
    # ответ PostgREST), выходим — иначе TOCTOU между pgrep и запуском мог бы
    # стартовать второй параллельный cloakbrowser.
    if not sb_patch_claim(jid):
        log("задачу уже взял другой экземпляр поллера", queue_id=jid)
        return 0
    log("беру задачу", queue_id=jid, query=query or "(полный прогон)")

    cmd = [PYTHON, PARSER, "--trigger", "dashboard"]
    if query:
        cmd += ["--only-query", query]

    try:
        # Парсер может идти до 25 мин; ставим запас 40 мин.
        res = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=2400)
        ok = res.returncode == 0
        sb_patch(f"/rest/v1/parse_queue?id=eq.{jid}", {
            "status": "done" if ok else "failed",
            "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "note": f"rc={res.returncode}; {(res.stderr or res.stdout or '')[-200:]}",
        })
        log("задача завершена", queue_id=jid, rc=res.returncode)
    except subprocess.TimeoutExpired:
        sb_patch(f"/rest/v1/parse_queue?id=eq.{jid}", {
            "status": "failed",
            "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "note": "timeout 2400s",
        })
        log("задача timeout", queue_id=jid)
    except BaseException as e:
        # любое иное прерывание (кроме жёсткого SIGKILL — его подберёт dead-letter
        # recovery при следующем тике): помечаем failed, чтобы задача не зависла
        try:
            sb_patch(f"/rest/v1/parse_queue?id=eq.{jid}", {
                "status": "failed",
                "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "note": f"прервано: {type(e).__name__}: {str(e)[:150]}",
            })
        except Exception:
            pass
        log("задача прервана", queue_id=jid, err=str(e)[:150])
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())

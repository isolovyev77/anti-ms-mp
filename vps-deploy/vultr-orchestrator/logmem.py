#!/usr/bin/env python3
"""Общий оперативный лог anti-ms-mp — координация между ИИ-агентами проекта.

Дозапись одной строки + ротация (шапка + последние KEEP записей) + дедуп подряд
идущих одинаковых строк (защита от рекурсивного спама). Центральный файл живёт на
Vultr: /opt/anti-ms-mp-watcher/memory.md. Читают/пишут: оркестратор (04:00 МСК),
утренняя рутина (ноутбук 09:13), сессии Claude Code.

Использование:
  logmem.py <source> <level> <message...>
    source: orchestrator | routine | claude | parser | ...
    level:  INFO | FIX | WARN | ERROR | NEEDS-CHECK | RESOLVED
Чтение: просто cat / tail файла memory.md.
"""
import sys, os, datetime as dt, fcntl

LOG = os.environ.get("ANTIMS_MEMORY", os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.md"))
KEEP = int(os.environ.get("ANTIMS_MEMORY_KEEP", "200"))
HEADER = f"""# anti-ms-mp — оперативный лог (общий для ИИ-агентов проекта)
#
# Читают/пишут: оркестратор (Vultr, 04:00 МСК), утренняя рутина (ноутбук, 09:13),
# сессии Claude Code. ПЕРЕД действиями по проекту прочитай последние записи (tail),
# чтобы НЕ дублировать уже сделанное и заметить пометки NEEDS-CHECK от оркестратора.
#
# Формат строки: [ISO-UTC] [источник] [LEVEL] текст.
# LEVEL: INFO|FIX|WARN|ERROR|NEEDS-CHECK|RESOLVED.
# Ротация: шапка + последние {KEEP} записей (старое отсекается). Дедуп подряд идущих.
# Писать: logmem.py <source> <level> <текст>  (на Vultr: /opt/anti-ms-mp-watcher/logmem.py)
---
"""


def _core(line: str) -> str:
    # отрезаем [timestamp] в начале для дедупа по содержанию
    return line.split("] ", 1)[1] if "] " in line else line


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: logmem.py <source> <level> <message...>")
        return 2
    source, level = sys.argv[1], sys.argv[2].upper()
    msg = " ".join(sys.argv[3:]).replace("\n", " ").strip()
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] [{source}] [{level}] {msg}"

    # #23: конкурентные писатели (оркестратор 04:00, рутина 09:13, сессии Claude)
    # могут пересечься на read-modify-write и затереть чужую запись. Берём
    # эксклюзивный flock на отдельный .lock — сериализует всю секцию ниже.
    lock = open(LOG + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)

        body = []
        if os.path.exists(LOG):
            txt = open(LOG, encoding="utf-8").read()
            body = (txt.split("---\n", 1)[1] if "---\n" in txt else txt).splitlines()
        body = [l for l in body if l.strip()]

        if body and _core(body[-1]) == _core(line):  # дедуп подряд идущих
            return 0
        body.append(line)
        body = body[-KEEP:]  # ротация

        tmp = LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(HEADER + "\n".join(body) + "\n")
        os.replace(tmp, LOG)  # атомарная запись
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())

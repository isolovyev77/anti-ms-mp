#!/usr/bin/env python3
"""Паритет-харнесс для дедупликации (аудит-фикс H1).

Грузит title_ok / official_price / EXTRACT_JS / PLATFORMS / OFFICIAL_PRICES /
BLOCK_MARKERS из python-файла БЕЗ выполнения import cloakbrowser (через AST —
вырезаем только нужные top-level узлы и exec их в изоляции). Сравнивает два
источника: статически (значения констант) и поведенчески (прогон фильтров по
реальной фикстуре title+url).

Использование:
  python parity.py <файл_A.py> <файл_B.py> <fixture.json>
Выход 0 если ПОЛНЫЙ паритет, иначе 1 + список расхождений.
"""
import ast
import json
import sys

SYMS = {"OFFICIAL_PRICES", "PLATFORMS", "EXTRACT_JS", "BLOCK_MARKERS",
        "title_ok", "official_price"}


def load(path):
    """exec нужных top-level узлов в изолированном namespace (без cloakbrowser)."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = " ".join(a.name for a in node.names)
            if "cloakbrowser" in mod or "cloakbrowser" in names:
                continue  # пропускаем тяжёлую зависимость
            keep.append(node)
        elif isinstance(node, ast.Assign):
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in SYMS:
                keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in SYMS:
            keep.append(node)
    mod = ast.Module(body=keep, type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {}
    exec(compile(mod, path, "exec"), ns)
    return ns


def platforms_sig(p):
    """PLATFORMS содержит lambda (url) — сравниваем по выходу, не по объекту."""
    out = {}
    for k, cfg in p.items():
        out[k] = {
            "max_pages": cfg.get("max_pages"),
            "card_selector": cfg.get("card_selector"),
            "url": cfg["url"]("Тест Q", 2),  # детерминированный сэмпл
        }
    return out


def main():
    a = load(sys.argv[1])
    b = load(sys.argv[2])
    fixture = json.load(open(sys.argv[3], encoding="utf-8"))
    fails = []

    # --- статический паритет констант ---
    if a["EXTRACT_JS"] != b["EXTRACT_JS"]:
        for k in a["EXTRACT_JS"]:
            if a["EXTRACT_JS"][k] != b["EXTRACT_JS"].get(k):
                fails.append(f"EXTRACT_JS['{k}'] различается ({len(a['EXTRACT_JS'][k])} vs "
                             f"{len(b['EXTRACT_JS'].get(k,''))} симв)")
    if a["OFFICIAL_PRICES"] != b["OFFICIAL_PRICES"]:
        fails.append("OFFICIAL_PRICES различаются")
    if list(a["BLOCK_MARKERS"]) != list(b["BLOCK_MARKERS"]):
        fails.append("BLOCK_MARKERS различаются")
    if platforms_sig(a["PLATFORMS"]) != platforms_sig(b["PLATFORMS"]):
        fails.append("PLATFORMS различаются (url/max_pages/selector)")

    # --- поведенческий паритет по фикстуре ---
    dt = do = 0
    examples = []
    for r in fixture:
        t = r.get("title") or ""
        u = r.get("url") or ""
        if a["title_ok"](t, u) != b["title_ok"](t, u):
            dt += 1
            if len(examples) < 5:
                examples.append(f"title_ok: A={a['title_ok'](t,u)} B={b['title_ok'](t,u)} | {t[:50]}")
        if a["official_price"](t) != b["official_price"](t):
            do += 1
            if len(examples) < 10:
                examples.append(f"official_price: A={a['official_price'](t)} B={b['official_price'](t)} | {t[:50]}")
    if dt:
        fails.append(f"title_ok расходится на {dt}/{len(fixture)} строк")
    if do:
        fails.append(f"official_price расходится на {do}/{len(fixture)} строк")

    print(f"=== ПАРИТЕТ {sys.argv[1].split('/')[-1]} ↔ {sys.argv[2].split('/')[-1]} "
          f"(фикстура {len(fixture)}) ===")
    if not fails:
        print("✅ ПОЛНЫЙ ПАРИТЕТ: константы и поведение идентичны")
        return 0
    print("РАСХОЖДЕНИЯ:")
    for f in fails:
        print("  ✗", f)
    for e in examples:
        print("    ·", e)
    return 1


if __name__ == "__main__":
    sys.exit(main())

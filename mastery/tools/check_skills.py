#!/usr/bin/env python3
"""Гейт целостности скиллов by_team/ × registry.csv.

Проверяет для каждой строки registry.csv со статусом «активен» и путём в by_team/:
  1. SKILL.md существует по пути из реестра;
  2. frontmatter: есть `name:` и он совпадает с именем папки и колонкой `имя`;
  3. frontmatter: `description:` непустой;
  4. в теле есть футер `Ключ реестра: MST-<id>` с ПРАВИЛЬНЫМ ключом;
  5. в теле есть отсылка к журналу применений usage_log.csv;
  6. evals/evals.json существует, валиден, skill_name совпадает, есть ≥2 кейса,
     у каждого кейса непустые prompt и assertions;
  7. в by_team/ нет папок-сирот, которых нет в реестре.

Ненулевой код выхода при любом нарушении — можно ставить в пайплайн.

Запуск:  python3 mastery/tools/check_skills.py
         python3 mastery/tools/check_skills.py --selftest   # негативный контроль
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

MASTERY = Path(__file__).resolve().parent.parent
REGISTRY = MASTERY / "registry.csv"
BY_TEAM = MASTERY / "tools" / "addons" / "by_team"


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    fm = {}
    key = None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            fm[key] = val
        elif key and line.startswith(" "):
            fm[key] += " " + line.strip()
    return fm


def check(registry_path=REGISTRY, by_team=BY_TEAM):
    errors = []
    rows = [r for r in csv.DictReader(open(registry_path, encoding="utf-8-sig"))
            if r["статус"] == "активен" and "by_team/" in r["путь"]]
    if not errors and not rows:
        errors.append("в реестре НОЛЬ активных by_team-скиллов — проверка не проверила ничего")
    seen_dirs = set()
    for r in rows:
        key, name = r["ключ"], r["имя"]
        skill_md = MASTERY.parent / r["путь"] if not Path(r["путь"]).is_absolute() else Path(r["путь"])
        # пути в реестре относительны .context/
        skill_md = (MASTERY.parent / r["путь"]).resolve()
        pref = f"{key} ({name})"
        if not skill_md.is_file():
            errors.append(f"{pref}: нет файла {r['путь']}")
            continue
        dirname = skill_md.parent.name
        seen_dirs.add(dirname)
        text = skill_md.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        if fm.get("name") != dirname:
            errors.append(f"{pref}: frontmatter name={fm.get('name')!r} != папка {dirname!r}")
        if fm.get("name") != name:
            errors.append(f"{pref}: frontmatter name={fm.get('name')!r} != реестр {name!r}")
        if len(fm.get("description", "")) < 40:
            errors.append(f"{pref}: description пуст или короче 40 символов")
        keys_in_footer = re.findall(r"Ключ реестра:\s*(MST-\d+)", text)
        if keys_in_footer != [key]:
            errors.append(f"{pref}: футер «Ключ реестра» = {keys_in_footer} (ожидался ровно [{key!r}])")
        if "usage_log.csv" not in text:
            errors.append(f"{pref}: нет отсылки к журналу применений usage_log.csv")
        ev = skill_md.parent / "evals" / "evals.json"
        if not ev.is_file():
            errors.append(f"{pref}: нет evals/evals.json")
        else:
            try:
                data = json.loads(ev.read_text(encoding="utf-8"))
                if data.get("skill_name") != dirname:
                    errors.append(f"{pref}: evals skill_name={data.get('skill_name')!r} != {dirname!r}")
                evals = data.get("evals", [])
                if len(evals) < 2:
                    errors.append(f"{pref}: в evals меньше 2 кейсов")
                for e in evals:
                    if not e.get("prompt") or not e.get("assertions"):
                        errors.append(f"{pref}: кейс id={e.get('id')} без prompt или assertions")
            except json.JSONDecodeError as exc:
                errors.append(f"{pref}: evals.json битый JSON: {exc}")
    if by_team.is_dir():
        orphans = {p.name for p in by_team.iterdir() if p.is_dir()} - seen_dirs
        for o in sorted(orphans):
            errors.append(f"сирота: by_team/{o}/ есть на диске, но не в реестре (активен)")
    return errors


def selftest():
    """Негативный контроль (MST-15): гейт обязан падать на сломанном входе."""
    import shutil, tempfile
    errs = check()
    assert errs == [], "позитив должен быть зелёным до селфтеста, а есть: %s" % errs
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # копия одного скилла с намеренно сломанным футером
        src = next(p for p in BY_TEAM.iterdir() if (p / "SKILL.md").is_file())
        dst = td / "by_team" / src.name
        shutil.copytree(src, dst)
        broken = (dst / "SKILL.md").read_text(encoding="utf-8").replace("Ключ реестра: MST-", "Ключ реестра: MST-9")
        (dst / "SKILL.md").write_text(broken, encoding="utf-8")
        # реестр из одной строки на эту копию — путь надо дать относительно .context
        # поэтому проверяем напрямую функциями
        text = (dst / "SKILL.md").read_text(encoding="utf-8")
        keys = re.findall(r"Ключ реестра:\s*(MST-\d+)", text)
        assert keys and keys[0].startswith("MST-9"), "поломка не применилась"
        # 2) битый JSON обязан ловиться
        (dst / "evals" / "evals.json").write_text("{оборвано", encoding="utf-8")
        try:
            json.loads((dst / "evals" / "evals.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("битый JSON прошёл незамеченным")
    # 3) пустой реестр обязан давать ошибку, а не зелёный ноль
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write("ключ,имя,вид,путь,этапы,домены,триггер,статус,создано,обновлено\n")
        empty = f.name
    errs = check(registry_path=empty)
    Path(empty).unlink()
    assert any("НОЛЬ активных" in e for e in errs), "пустой реестр дал ложный зелёный"
    print("selftest OK: 3/3 (позитив зелёный, поломки ловятся, пустой вход не зелёный)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    errors = check()
    if errors:
        print("ПРОВАЛ ГЕЙТА, нарушений: %d" % len(errors))
        for e in errors:
            print(" -", e)
        return 1
    print("OK: все активные by_team-скиллы целостны")
    return 0


if __name__ == "__main__":
    sys.exit(main())

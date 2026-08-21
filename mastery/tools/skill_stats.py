#!/usr/bin/env python3
"""Боевая метрика скиллов: агрегат mastery/usage_log.csv × mastery/registry.csv.

Зачем: понимать, какие скиллы реально работают, а какие — мёртвый груз.
Скилл без применений не «плохой», но и не проверенный; скилл с низким
hit-rate — хуже отсутствия скилла (тратит контекст, не помогает).

Метрики на скилл (MST-ключ):
  применений        — всего строк в usage_log.csv;
  за 30 дней        — применений за последние 30 дней;
  hit-rate          — (помог + 0.5*частично) / применений;
  последнее         — дата последнего применения;
  вердикт           — правило см. VERDICT_RULES ниже.

Вердикты:
  боевой            — ≥3 применений и hit-rate ≥ 0.7;
  наблюдаем         — всё остальное (мало данных);
  на списание       — hit-rate < 0.5 при ≥4 применениях,
                      ЛИБО 0 применений за 42 дня при возрасте скилла ≥42 дней.
«На списание» — не удаление: снять симлинк с витрины, статус в registry.csv
→ «на списание», решение об архиве принимает владелец (rule-10 для устава,
здесь — аналогичная логика: агент предлагает, человек решает).

Словарь исходов: помог | частично | не_помог | вреден
(«вреден» = скилл направил не туда, считается как 0 в hit-rate и
подсвечивается отдельно).

Запуск:  python3 mastery/tools/skill_stats.py            # отчёт в stdout (markdown)
         python3 mastery/tools/skill_stats.py --selftest # негативный контроль

Файлы только читаются — лок не нужен.
"""

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

MASTERY = Path(__file__).resolve().parent.parent
USAGE = MASTERY / "usage_log.csv"
REGISTRY = MASTERY / "registry.csv"

OUTCOME_WEIGHT = {"помог": 1.0, "частично": 0.5, "не_помог": 0.0, "вреден": 0.0}

WINDOW_RECENT = timedelta(days=30)
WINDOW_STALE = timedelta(days=42)


def parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"непонятная дата: {s!r} (ожидаю YYYY-MM-DD [HH:MM])")


def load_usage(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        need = {"дата", "скилл", "исход"}
        if not need.issubset(set(reader.fieldnames or [])):
            raise SystemExit(f"usage_log.csv: нет колонок {need - set(reader.fieldnames or [])}")
        for i, r in enumerate(reader, start=2):
            outcome = (r["исход"] or "").strip()
            if outcome not in OUTCOME_WEIGHT:
                raise SystemExit(
                    f"usage_log.csv строка {i}: исход {outcome!r} вне словаря {sorted(OUTCOME_WEIGHT)}"
                )
            rows.append({
                "dt": parse_dt(r["дата"]),
                "skill": r["скилл"].strip(),
                "outcome": outcome,
                "task": (r.get("задача") or "").strip(),
            })
    return rows


def load_registry(path):
    skills = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            skills[r["ключ"].strip()] = {
                "name": r["имя"].strip(),
                "status": r["статус"].strip(),
                "created": parse_dt(r["создано"]),
            }
    return skills


def verdict(n_total, hit_rate, last_dt, created, now):
    if n_total >= 4 and hit_rate is not None and hit_rate < 0.5:
        return "на списание"
    if n_total == 0 and (now - created) >= WINDOW_STALE:
        return "на списание"
    if last_dt is not None and (now - last_dt) >= WINDOW_STALE and (now - created) >= WINDOW_STALE:
        return "на списание"
    if n_total >= 3 and hit_rate is not None and hit_rate >= 0.7:
        return "боевой"
    return "наблюдаем"


def build_report(usage, registry, now):
    lines = ["# Боевая метрика скиллов", "",
             f"Снято: {now:%Y-%m-%d %H:%M}. Правила вердиктов — в шапке skill_stats.py.", "",
             "| Ключ | Скилл | Статус | Применений | За 30 дн | Hit-rate | Последнее | Вердикт |",
             "|---|---|---|---|---|---|---|---|"]
    unknown = sorted({u["skill"] for u in usage} - set(registry))
    harmful = [u for u in usage if u["outcome"] == "вреден"]
    for key in sorted(registry, key=lambda k: int(k.split("-")[1])):
        meta = registry[key]
        if meta["status"] not in ("активен", "на списание"):
            continue
        mine = [u for u in usage if u["skill"] == key]
        n = len(mine)
        recent = sum(1 for u in mine if now - u["dt"] <= WINDOW_RECENT)
        hr = (sum(OUTCOME_WEIGHT[u["outcome"]] for u in mine) / n) if n else None
        last = max((u["dt"] for u in mine), default=None)
        v = verdict(n, hr, last, meta["created"], now)
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            key, meta["name"], meta["status"], n, recent,
            f"{hr:.0%}" if hr is not None else "—",
            f"{last:%Y-%m-%d}" if last else "—", v))
    if harmful:
        lines += ["", "## Отметки «вреден» — разобрать поимённо", ""]
        lines += [f"- {u['dt']:%Y-%m-%d} {u['skill']}: {u['task']}" for u in harmful]
    if unknown:
        lines += ["", f"ВНИМАНИЕ: в usage_log.csv есть ключи вне registry.csv: {', '.join(unknown)}"]
    return "\n".join(lines)


def selftest():
    """Негативный контроль (MST-15): проверки обязаны падать на сломанном входе."""
    now = datetime(2026, 9, 1)
    reg = {"MST-99": {"name": "t", "status": "активен", "created": now - timedelta(days=60)}}
    # 1) свежий боевой
    assert verdict(3, 1.0, now, now - timedelta(days=1), now) == "боевой"
    # 2) провальный hit-rate -> на списание
    assert verdict(4, 0.25, now, now - timedelta(days=1), now) == "на списание"
    # 3) 0 применений за 42 дня при старом скилле -> на списание
    assert verdict(0, None, None, now - timedelta(days=60), now) == "на списание"
    # 4) молодой без применений -> наблюдаем
    assert verdict(0, None, None, now - timedelta(days=5), now) == "наблюдаем"
    # 5) неизвестный исход обязан валить загрузку
    import tempfile, os
    bad = "дата,скилл,кто,задача,исход,заметка\n2026-08-30 10:00,MST-99,x,t,шикарно,\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(bad)
        p = f.name
    try:
        try:
            load_usage(p)
        except SystemExit:
            pass
        else:
            raise AssertionError("битый исход НЕ уронил загрузку — негативный контроль провален")
    finally:
        os.unlink(p)
    # 6) отчёт подсвечивает ключ вне реестра
    usage = [{"dt": now, "skill": "MST-77", "outcome": "помог", "task": "t"}]
    assert "MST-77" in build_report(usage, reg, now)
    print("selftest OK: 6/6")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    now = datetime.now()
    print(build_report(load_usage(USAGE), load_registry(REGISTRY), now))


if __name__ == "__main__":
    sys.exit(main())

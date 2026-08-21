#!/usr/bin/env python3
"""Мьютексы на файлы .context: один запуск работает с файлом одновременно.

Механика: атомарный mkdir в mastery/.locks/. Захватил каталог — владеешь локом.
Внутри лежит owner.json (кто, pid, когда, что). Протухший лок (TTL истёк И
владелец-процесс мёртв) можно перехватить — перехват пишется в stderr.

Использование:
    ctx_lock.py acquire tracker/tasks.csv [--wait 30] [--ttl 1800] [--owner имя]
    ctx_lock.py release tracker/tasks.csv
    ctx_lock.py list
    ctx_lock.py clean          # снять все протухшие локи

Коды выхода: 0 — успех; 1 — занято/ошибка.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

CTX = Path(__file__).resolve().parent.parent  # mastery/
LOCKS = CTX / ".locks"
DEFAULT_TTL = 1800  # 30 минут


def lock_dir(target: str) -> Path:
    rel = os.path.normpath(target).replace("/", "__")
    return LOCKS / f"{rel}.lock"


def read_owner(ld: Path) -> dict:
    try:
        return json.loads((ld / "owner.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def is_stale(ld: Path, ttl: int) -> bool:
    meta = read_owner(ld)
    try:
        age = time.time() - float(meta.get("ts", ld.stat().st_mtime))
    except OSError:
        return False  # лок исчез — не нам решать
    return age > ttl and not pid_alive(meta.get("pid"))


def acquire(target: str, wait: int, ttl: int, owner: str) -> int:
    LOCKS.mkdir(exist_ok=True)
    ld = lock_dir(target)
    deadline = time.time() + wait
    while True:
        try:
            ld.mkdir()  # атомарно: либо наш, либо занят
            meta = {
                "owner": owner,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "target": target,
                "ts": time.time(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (ld / "owner.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            print(f"LOCKED {target} <- {owner} (pid {os.getpid()})")
            return 0
        except FileExistsError:
            if is_stale(ld, ttl):
                held = read_owner(ld)
                print(
                    f"перехват протухшего лока {target}: владелец "
                    f"{held.get('owner')} pid {held.get('pid')} мёртв, "
                    f"ttl {ttl}s истёк",
                    file=sys.stderr,
                )
                shutil.rmtree(ld, ignore_errors=True)
                continue
            if time.time() >= deadline:
                held = read_owner(ld)
                print(
                    f"BUSY {target}: держит {held.get('owner', '?')} "
                    f"pid {held.get('pid', '?')} с {held.get('time', '?')}",
                    file=sys.stderr,
                )
                return 1
            time.sleep(0.5)


def release(target: str) -> int:
    ld = lock_dir(target)
    if not ld.exists():
        print(f"нет лока на {target}", file=sys.stderr)
        return 1
    shutil.rmtree(ld)
    print(f"RELEASED {target}")
    return 0


def list_locks() -> int:
    if not LOCKS.is_dir() or not any(LOCKS.iterdir()):
        print("локов нет")
        return 0
    for ld in sorted(LOCKS.iterdir()):
        m = read_owner(ld)
        alive = "жив" if pid_alive(m.get("pid")) else "МЁРТВ"
        print(
            f"{m.get('target', ld.name):40} {m.get('owner', '?'):12} "
            f"pid {m.get('pid', '?')} ({alive}) с {m.get('time', '?')}"
        )
    return 0


def clean(ttl: int) -> int:
    if not LOCKS.is_dir():
        return 0
    n = 0
    for ld in list(LOCKS.iterdir()):
        if is_stale(ld, ttl):
            print(f"снят протухший лок: {read_owner(ld).get('target', ld.name)}")
            shutil.rmtree(ld, ignore_errors=True)
            n += 1
    print(f"снято: {n}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cmd", choices=["acquire", "release", "list", "clean"])
    p.add_argument("target", nargs="?", help="путь относительно .context/")
    p.add_argument("--wait", type=int, default=0, help="сколько секунд ждать занятый лок")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="секунд до протухания")
    p.add_argument("--owner", default=os.environ.get("CTX_AGENT", f"user:{os.getlogin() if hasattr(os, 'getlogin') else '?'}"))
    a = p.parse_args()
    if a.cmd in ("acquire", "release") and not a.target:
        p.error(f"{a.cmd} требует target")
    if a.cmd == "acquire":
        return acquire(a.target, a.wait, a.ttl, a.owner)
    if a.cmd == "release":
        return release(a.target)
    if a.cmd == "list":
        return list_locks()
    return clean(a.ttl)


if __name__ == "__main__":
    sys.exit(main())

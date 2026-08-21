#!/usr/bin/env python3
"""Проверка ссылок на файлы в документах .context.

Ищет в *.md и *.csv токены-пути (обязательно со слэшем — голые имена файлов
считаются упоминанием конвенции, не ссылкой) и проверяет существование.
Ловит битые ссылки после реорганизаций структуры.

Будущие артефакты (обещанные планами, ещё не созданные) заносятся
в check_refs_allow.txt — по паттерну fnmatch на строку.

Исключения: work_journal.md (append-only история), внешние клоны в
mastery/tools/addons/source/, URL, шаблоны с <>{}*.

Выход: 0 — чисто; 1 — есть битые ссылки.
"""

import fnmatch
import re
import sys
from pathlib import Path

CTX = Path(__file__).resolve().parent.parent.parent  # .context/
WORKSPACE = CTX.parent
ALLOW_FILE = Path(__file__).parent / "check_refs_allow.txt"

SKIP_PARTS = {".git", "__pycache__", ".locks"}
SKIP_REL = ("mastery/tools/addons/source/", "mastery/tools/saturday-tracker/")
SKIP_FILES = {"work_journal.md"}

TOKEN = re.compile(
    r"[A-Za-z0-9_а-яА-ЯёЁ][\w.\-а-яА-ЯёЁ]*(?:/[\w.\-а-яА-ЯёЁ]+)+"
    r"\.(?:md|csv|py|sql|xlsx|json|yml|xml)\b"
)


def load_allow():
    if not ALLOW_FILE.exists():
        return []
    pats = []
    for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line)
    return pats


def iter_docs():
    for p in CTX.rglob("*"):
        if p.suffix not in (".md", ".csv") or not p.is_file():
            continue
        rel = p.relative_to(CTX).as_posix()
        if p.name in SKIP_FILES or any(rel.startswith(s) for s in SKIP_REL):
            continue
        if SKIP_PARTS & set(p.parts):
            continue
        yield p


def resolves(token: str, base: Path) -> bool:
    tok = token
    # '.context/x' матчится токенайзером как 'context/x' — вернём смысл
    if tok.startswith("context/"):
        tok = tok[len("context/"):]
        return (CTX / tok).exists()
    # при необходимости добавьте сюда корни соседних репозиториев workspace
    roots = [base.parent, CTX, WORKSPACE]
    hidden = "." + tok
    return any((r / tok).exists() or (r / hidden).exists() for r in roots)


def main() -> int:
    allow = load_allow()
    broken = []
    for doc in iter_docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            line = re.sub(r"https?://\S+", "", line)
            for m in TOKEN.finditer(line):
                tok = m.group(0)
                ctxt = line[max(0, m.start() - 1):m.end() + 1]
                if any(ch in ctxt for ch in "<>{}*"):
                    continue
                if any(fnmatch.fnmatch(tok, p) for p in allow):
                    continue
                if not resolves(tok, doc):
                    broken.append((doc.relative_to(CTX).as_posix(), line_no, tok))
    if broken:
        print(f"БИТЫХ ССЫЛОК: {len(broken)}")
        for f, ln, tok in broken:
            print(f"  {f}:{ln}: {tok}")
        return 1
    print("все ссылки разрешаются")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Дамп листа .xlsx в CSV средствами одной stdlib (zipfile + ElementTree).

Зачем: openpyxl бывает недоступен (окружение без pip, запрет на зависимости,
разовый разбор чужой выгрузки). .xlsx — это zip с XML, читается напрямую.

Использование:
    python3 xlsx_dump.py файл.xlsx                 # первый лист
    python3 xlsx_dump.py файл.xlsx "Лист1"         # лист по имени
    python3 xlsx_dump.py файл.xlsx --list          # перечислить листы
    python3 xlsx_dump.py файл.xlsx --raw           # не конвертировать даты

Вывод: CSV в stdout (utf-8). Пустые ячейки — пустые поля, позиция берётся
из атрибута r ячейки, а не из порядка следования.
"""
import argparse
import csv
import datetime as dt
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

# Основное пространство имён SpreadsheetML. ElementTree возвращает теги
# в виде "{ns}tag", поэтому сравнивать с голым "row"/"c" бесполезно.
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": NS_MAIN, "r": NS_REL}

# Числовые форматы (numFmtId), которые Excel считает датой/временем.
BUILTIN_DATE_FMT = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
DATE_TOKEN = re.compile(r"(?<!\\)[yYmMdDhHsS]")
CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def col_to_index(col: str) -> int:
    """A -> 0, B -> 1, ..., Z -> 25, AA -> 26."""
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def local(tag: str) -> str:
    """Отрезать {namespace} от имени тега."""
    return tag.rsplit("}", 1)[-1]


def read_shared_strings(zf: zipfile.ZipFile) -> list:
    """sharedStrings.xml: ячейки с t="s" хранят ИНДЕКС в этой таблице."""
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []  # файл без строковых литералов — легальный случай
    out = []
    for si in ET.fromstring(raw).findall("m:si", NS):
        # Текст может быть разбит на несколько <t> внутри <r> (rich text),
        # поэтому собираем все <t> поддерева, а не только прямой si/t.
        out.append("".join(t.text or "" for t in si.iter(f"{{{NS_MAIN}}}t")))
    return out


def read_date_styles(zf: zipfile.ZipFile) -> set:
    """Индексы стилей (атрибут s ячейки), означающие дату.

    Без styles.xml дату не отличить от числа: в XML и то и другое — <v>45000</v>.
    """
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom = {}
    for nf in root.iter(f"{{{NS_MAIN}}}numFmt"):
        code = nf.get("formatCode", "")
        custom[int(nf.get("numFmtId"))] = bool(DATE_TOKEN.search(code))
    date_styles = set()
    cell_xfs = root.find("m:cellXfs", NS)
    if cell_xfs is None:
        return date_styles
    for i, xf in enumerate(cell_xfs.findall("m:xf", NS)):
        fmt_id = int(xf.get("numFmtId", 0))
        if fmt_id in BUILTIN_DATE_FMT or custom.get(fmt_id):
            date_styles.add(i)
    return date_styles


def serial_to_date(value: float) -> str:
    """Excel-serial -> ISO. Эпоха 1899-12-30 из-за бага «високосного 1900».

    Excel считает 1900 год високосным (serial 60 = несуществующее 1900-02-29),
    поэтому база сдвинута на два дня назад относительно 1900-01-01.
    """
    if value < 0:
        return str(value)
    base = dt.datetime(1899, 12, 30)
    try:
        stamp = base + dt.timedelta(days=value)
    except OverflowError:
        return str(value)
    if abs(value - int(value)) < 1e-9:
        return stamp.date().isoformat()
    return stamp.replace(microsecond=0).isoformat(sep=" ")


def sheet_targets(zf: zipfile.ZipFile) -> list:
    """[(имя листа, путь к xml)] в порядке книги — через r:id и workbook.xml.rels.

    Имя sheetN.xml НЕ обязано совпадать с sheetId, порядок в zip произвольный.
    """
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    by_id = {}
    for rel in rels:
        target = rel.get("Target", "")
        target = target[1:] if target.startswith("/") else "xl/" + target.lstrip("./")
        by_id[rel.get("Id")] = target
    out = []
    for sheet in wb.iter(f"{{{NS_MAIN}}}sheet"):
        rid = sheet.get(f"{{{NS_REL}}}id")
        out.append((sheet.get("name"), by_id.get(rid)))
    return out


def numeric_value(cell, text: str, date_styles, raw: bool) -> str:
    """Число или дата: тип решает стиль ячейки, а не само значение."""
    try:
        num = float(text)
    except ValueError:
        return text
    style = cell.get("s")
    if not raw and style is not None and int(style) in date_styles:
        return serial_to_date(num)
    return str(int(num)) if num == int(num) else str(num)


def cell_value(cell, shared, date_styles, raw: bool) -> str:
    """Значение ячейки строкой с учётом её типа (атрибут t)."""
    ctype = cell.get("t", "n")
    if ctype == "inlineStr":
        node = cell.find("m:is", NS)
        if node is None:
            return ""
        return "".join(t.text or "" for t in node.iter(f"{{{NS_MAIN}}}t"))
    v = cell.find("m:v", NS)
    if v is None or v.text is None:
        return ""
    text = v.text
    if ctype == "s":  # индекс в sharedStrings, а не сам текст
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return text
    if ctype in ("str", "e"):  # результат формулы / код ошибки
        return text
    if ctype == "b":
        return "TRUE" if text == "1" else "FALSE"
    return numeric_value(cell, text, date_styles, raw)  # ctype "n" или отсутствует


def pick_sheet(zf: zipfile.ZipFile, sheet_name):
    """(имя, путь) выбранного листа либо (None, сообщение об ошибке)."""
    sheets = sheet_targets(zf)
    if not sheets:
        return None, "не найдено ни одного листа"
    if sheet_name is None:
        name, target = sheets[0]
    else:
        match = [s for s in sheets if s[0] == sheet_name]
        if not match:
            have = ", ".join(repr(s[0]) for s in sheets)
            return None, f"лист {sheet_name!r} не найден; есть: {have}"
        name, target = match[0]
    if target is None or target not in zf.namelist():
        return None, f"лист {name!r}: нет файла {target}"
    return target, None


def parse_rows(zf, target, shared, date_styles, raw):
    """Строки листа как словари {индекс колонки: значение} + фактическая ширина."""
    rows, width = [], 0
    # iterparse + clear(): лист на сотни тысяч строк не держим в памяти целиком
    with zf.open(target) as stream:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            if local(elem.tag) != "row":
                continue
            row = {}
            for cell in elem:
                if local(cell.tag) != "c":
                    continue
                ref = cell.get("r")
                m = CELL_REF.match(ref) if ref else None
                # Пустые ячейки в XML ОТСУТСТВУЮТ: без r по порядку
                # значения уехали бы влево на число пропусков.
                idx = col_to_index(m.group(1)) if m else len(row)
                val = cell_value(cell, shared, date_styles, raw)
                if val != "":
                    row[idx] = val
            if row:
                width = max(width, max(row) + 1)
            rows.append(row)
            elem.clear()
    return rows, width


def dump(path: str, sheet_name=None, raw: bool = False) -> int:
    with zipfile.ZipFile(path) as zf:
        target, err = pick_sheet(zf, sheet_name)
        if err:
            print(f"{path}: {err}" if target is None and sheet_name is None else err, file=sys.stderr)
            return 1
        shared = read_shared_strings(zf)
        date_styles = set() if raw else read_date_styles(zf)
        rows, width = parse_rows(zf, target, shared, date_styles, raw)
        writer = csv.writer(sys.stdout, lineterminator="\n")
        for row in rows:
            # ширина по факту: dimension ref часто устаревает после правок файла
            writer.writerow([row.get(i, "") for i in range(width)])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Дамп листа .xlsx в CSV (только stdlib)")
    ap.add_argument("xlsx", help="путь к .xlsx")
    ap.add_argument("sheet", nargs="?", default=None, help="имя листа (по умолчанию первый)")
    ap.add_argument("--list", action="store_true", help="перечислить листы и выйти")
    ap.add_argument("--raw", action="store_true", help="не конвертировать serial-числа в даты")
    args = ap.parse_args()
    try:
        if args.list:
            with zipfile.ZipFile(args.xlsx) as zf:
                for name, target in sheet_targets(zf):
                    print(f"{name}\t{target}")
            return 0
        return dump(args.xlsx, args.sheet, args.raw)
    except zipfile.BadZipFile:
        print(f"{args.xlsx}: не zip — это .xls или битый файл", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"{args.xlsx}: файл не найден", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Чтение и запись «Карты переноса» — файла, который открывают в Excel.

Формат согласован с прежней версией: разделитель `;`, кодировка UTF-8 с BOM.
Уверенность теперь раздельная — две колонки («имени» и «переноса») вместо одной,
всего 12 колонок; старую карту с единой «Уверенность» программа читает и
разкладывает значение в обе колонки автоматически. Служебные поля модели
(флаги, исправления, токены) в CSV не пишутся — они остаются в suggestions.json.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .config_loader import MAP_NAME

MAP_FIELDS = [
    "Текущий путь",
    "Текущее имя",
    "Тип операции",
    "Новое имя",
    "Целевой путь",
    "Путь ярлыка",
    "Уверенность имени",
    "Уверенность переноса",
    "Причина",
    "Применить",
    "Статус",
    "Комментарий",
    "Кто предложил",
]
LEGACY_CONF_FIELD = "Уверенность"
HUMAN_FIELDS = {"Комментарий", "Путь ярлыка", "Применить"}
STATUS_DONE = {"перенесено", "готово", "отказ", "проверка", "новое", "ошибка", "без изменений"}


def load_map(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        rows = []
        for raw in reader:
            row = {key: (raw.get(key) or "").strip() for key in MAP_FIELDS}
            if not row["Уверенность имени"] and not row["Уверенность переноса"]:
                legacy = (raw.get(LEGACY_CONF_FIELD) or "").strip()
                if legacy:
                    row["Уверенность имени"] = legacy
                    row["Уверенность переноса"] = legacy
            if row["Текущий путь"]:
                rows.append(row)
    return rows


def save_map(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MAP_FIELDS, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in MAP_FIELDS})
    tmp.replace(path)


APPLIED_NAME = "migration_map_applied.csv"


def split_applied(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Активные строки и те, что закрыты (перенесены или подтверждены «без изменений»)."""
    done, active = [], []
    for row in rows:
        (done if (row.get("Статус") or "").strip() in {"перенесено", "без изменений"} else active).append(row)
    return active, done


def archive_applied(output: Path) -> int:
    """Перенесённое убирается из активной карты в файл истории.

    Так карта показывает только то, что ещё ждёт решения, а журнал «что куда
    уехало» остаётся полным и открывается в Excel.
    """
    output = Path(output)
    rows = load_map(output / MAP_NAME)
    active, done = split_applied(rows)
    if not done:
        return 0
    history = load_map(output / APPLIED_NAME)
    known = {(row.get("Текущий путь") or "").replace("\\", "/") for row in history}
    moved = 0
    for row in done:
        old = (row.get("Текущий путь") or "").replace("\\", "/")
        parent = (row.get("Целевой путь") or "").strip("/") or old.rpartition("/")[0]
        new = f"{parent}/{row.get('Новое имя') or ''}".strip("/")
        record = dict(row)
        record["Текущий путь"] = new or old
        record["Применить"] = "FALSE"
        note = (row.get("Комментарий") or "").strip()
        record["Комментарий"] = (f"было: {old}" + (f" | {note}" if note else ""))[:400]
        if new and new.replace("\\", "/") not in known:
            history.append(record)
            known.add(new.replace("\\", "/"))
            moved += 1
    save_map(output / APPLIED_NAME, history)
    save_map(output / MAP_NAME, active)
    return moved


def archive_manual(output: Path, row: dict) -> int:
    """Положить в историю строку, подтверждённую человеком «без изменений» (без переноса).

    Файл остаётся на месте — в записи истории путь не меняется, только статус и
    пометка в комментарии. Дедупликация по текущему пути, чтобы не плодить копии.
    """
    output = Path(output)
    history = load_map(output / APPLIED_NAME)
    cur = (row.get("Текущий путь") or "").replace("\\", "/")
    if any((h.get("Текущий путь") or "").replace("\\", "/") == cur for h in history):
        return 0
    record = dict(row)
    record["Текущий путь"] = cur
    record["Статус"] = "без изменений"
    record["Тип операции"] = "без изменений"
    record["Применить"] = "FALSE"
    note = (row.get("Комментарий") or "").strip()
    record["Комментарий"] = ("подтверждён без изменений" + (f" | {note}" if note else ""))[:400]
    history.append(record)
    save_map(output / APPLIED_NAME, history)
    return 1


def as_map_row(record: dict, auto_apply: bool = True, source: str = "") -> dict:
    """Запись разбора → строка карты. Флаги и исправления приклеиваем к причине.

    auto_apply=False: «Применить» всегда FALSE — переносит только человек, отметив строку."""
    reason = str(record.get("Причина", ""))
    extras = []
    if record.get("_флаги"):
        extras.append("флаги: " + str(record["_флаги"]))
    if record.get("_исправления"):
        extras.append("исправлено: " + str(record["_исправления"]))
    if record.get("_ошибка"):
        extras.append("сбой: " + str(record["_ошибка"]))
    row = {key: record.get(key, "") for key in MAP_FIELDS}
    row["Кто предложил"] = str(record.get("Кто предложил") or source or "")[:120]
    row["Причина"] = " | ".join([reason] + extras)[:900] if extras else reason[:900]
    legacy = record.get(LEGACY_CONF_FIELD, "")
    for col in ("Уверенность имени", "Уверенность переноса"):
        val = row.get(col, "")
        if (val == "" or val is None) and legacy not in ("", None):
            val = legacy
        row[col] = "" if val in ("", None) else f"{val}"
    row["Применить"] = "TRUE" if auto_apply and str(row.get("Статус")) == "готово" and _auto(row) else "FALSE"
    return row


def _auto(row: dict) -> bool:
    """Автоприменение только для готовых строк с нормальным путём и именем."""
    return bool(row.get("Целевой путь") and row.get("Новое имя")) and str(row.get("Тип операции")) != "без изменений"


MANUAL_NAME = "manual_edits.json"


def _norm_rel(path: str) -> str:
    return (path or "").replace("\\", "/").strip("/")


def load_manual(output: Path) -> set[str]:
    """Пути, чьи имя/папку человек правил руками: повторный разбор их не перезапишет."""
    try:
        data = json.loads((Path(output) / MANUAL_NAME).read_text("utf-8"))
        return {_norm_rel(p) for p in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def mark_manual(output: Path, rel: str) -> None:
    rel = _norm_rel(rel)
    if not rel:
        return
    current = load_manual(output)
    if rel in current:
        return
    current.add(rel)
    target = Path(output) / MANUAL_NAME
    try:
        Path(output).mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(current), ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(target)
    except OSError:
        pass          # реестр — страховка; без него строка с комментарием/отметкой всё равно защищена


def is_touched(row: dict, manual: set[str] | None = None) -> bool:
    """Строку правил(а) человек: комментарий, ярлык, отметка «Применить»=TRUE, решение по ней
    или явная правка имени/папки (реестр manual_edits.json).

    «Применить»=FALSE — просто состояние по умолчанию, это не правка: иначе повторный разбор
    («разобрать заново», перезапуск после сбоя) не обновлял бы ни одной строки карты."""
    if (row.get("Комментарий") or "").strip() or (row.get("Путь ярлыка") or "").strip():
        return True
    if (row.get("Применить") or "").strip().upper() == "TRUE":
        return True
    if manual and _norm_rel(row.get("Текущий путь", "")) in manual:
        return True
    return (row.get("Статус") or "").strip() in {"перенесено", "отказ", "без изменений"}


def merge_suggestions(
    map_rows: list[dict],
    records: list[dict],
    overwrite: bool = False,
    manual: set[str] | None = None,
    auto_apply: bool = True,
    source: str = "",
) -> tuple[list[dict], int, int, int]:
    """Обновить карту результатами разбора, не затирая ручные правки.

    Возвращает (строки, добавлено, обновлено, пропущено).
    """
    index = {(row.get("Текущий путь") or "").replace("\\", "/"): row for row in map_rows}
    added = updated = skipped = 0
    for record in records:
        new_row = as_map_row(record, auto_apply=auto_apply, source=source)
        key = new_row["Текущий путь"].replace("\\", "/")
        old = index.get(key)
        if old is None:
            map_rows.append(new_row)
            index[key] = new_row
            added += 1
            continue
        touched = is_touched(old, manual)
        if touched and not overwrite:
            skipped += 1
            continue
        for field in MAP_FIELDS:
            if field in HUMAN_FIELDS and not overwrite:
                continue
            old[field] = new_row.get(field, old.get(field, ""))
        updated += 1
    map_rows.sort(key=lambda r: (str(r.get("Статус")), str(r.get("Текущий путь"))))
    return map_rows, added, updated, skipped


def load_records(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def stats(rows: list[dict]) -> dict:
    counter = {"всего": len(rows), "готово": 0, "проверка": 0, "перенесено": 0, "отмечено": 0, "новое": 0}
    for row in rows:
        status = (row.get("Статус") or "").strip()
        if status in counter:
            counter[status] += 1
        if (row.get("Применить") or "").strip().upper() == "TRUE":
            counter["отмечено"] += 1
    return counter

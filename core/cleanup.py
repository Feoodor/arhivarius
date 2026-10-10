"""Корзина отдела: предложение убрать лишнее и перемещение в папку удаления.

Программа сама ничего не стирает. Она собирает кандидатов по явным критериям
(пустой файл, битый файл, лишний дубликат, явный мусор по мнению модели) и
предлагает оператору. После подтверждения файлы ПЕРЕМЕЩАЮТСЯ в отдельную папку
(по умолчанию «99_Корзина на проверку» в корне отдела), откуда человек удаляет их
ручной операцией в проводнике. Перемещение записывается в тот же
operations_log.csv и откатывается тем же undo-журналом.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from .config_loader import MAP_NAME
from .mapio import load_map, save_map
from .plugins import GOOGLE_SHORTCUT, check_file
from .scanner import duplicate_groups, load_snapshot

TRASH_STATUS = "в корзину"
JUNK_WORDS = ("isco", "temp", "tmp", "безымянный", "noname", "untitled", "test", "проба",
              "черновик удален", "старое", "не нужно")


def _has_token(name: str, word: str) -> bool:
    """Слово как отдельный токен имени: «test-2.docx» — да, «latest-otchet.docx» — нет."""
    import re

    return re.search(rf"(?<![0-9a-z\u0430-\u044f]){re.escape(word)}(?![0-9a-z\u0430-\u044f])", name) is not None


def trash_dir(config: dict) -> Path:
    value = str(config.get("quarantine_dir", "")).strip()
    if value:
        path = Path(value)
        return path if path.is_absolute() else (config["scan_root"] / value)
    return config["scan_root"] / "99_Корзина на проверку"


def candidates(config: dict, snapshot: dict | None = None, limit: int = 0) -> list[dict]:
    """Список того, что стоит убрать. Только предложение, ничего не перемещает."""
    snapshot = snapshot or load_snapshot(config) or {"files": []}
    root: Path = config["scan_root"]
    rows: list[dict] = []
    seen: set[str] = set()
    try:  # то, что уже лежит в корзине отдела, повторно не предлагаем
        trash_rel = str(trash_dir(config).relative_to(root)).replace("\\", "/").casefold().strip("/")
    except (ValueError, OSError):
        trash_rel = ""

    def add(item: dict, reason: str, strength: str) -> None:
        rel = str(item.get("path", "")).replace("\\", "/")
        low = rel.casefold()
        if trash_rel and (low == trash_rel or low.startswith(trash_rel + "/")):
            return   # уже в корзине — не предлагаем второй раз
        if rel in seen:
            return
        seen.add(rel)
        rows.append({
            "Путь": rel,
            "Имя": Path(rel).name,
            "Размер": int(item.get("size") or 0),
            "Изменён": str(item.get("modified", ""))[:19],
            "Причина": reason,
            "Уверенность": strength,
            "Отметить": "FALSE",
        })

    for item in snapshot.get("files", []):
        rel = str(item.get("path", "")).replace("\\", "/")
        name = Path(rel).name.casefold()
        suffix = Path(rel).suffix.lower()
        if any(word in name for word in JUNK_WORDS if _has_token(name, word)):
            add(item, "в имени слово-признак мусора", "посмотреть")
        if int(item.get("size") or 0) == 0:
            if suffix in GOOGLE_SHORTCUT:
                continue  # ярлык Google Docs: 0 байт — это норма, сам документ в облаке
            add(item, "пустой файл", "пустой")
        if item.get("error"):
            add(item, f"ошибка чтения: {item['error']}", "битый")
        elif limit and len(rows) >= limit:
            break

    for digest, items in duplicate_groups(snapshot):
        keep = min(items, key=lambda i: (str(i["path"]).count("/"), str(i["path"])))
        for item in items:
            if item is keep:
                continue
            add(item, f"совпадает по содержимому с {keep['path']}", "дубликат")

    for row in list(rows):
        path = root / row["Путь"]
        if not path.exists():
            rows.remove(row)
            continue
        problem = check_file(path)
        if problem and row["Уверенность"] not in {"пустой", "битый"}:
            row["Причина"] = problem
            row["Уверенность"] = "битый"
    return rows


def mark_from_model(records: list[dict]) -> list[dict]:
    """Файлы, которые модель сама предложила выбросить (поле discard)."""
    rows = []
    for record in records:
        if not record.get("_выбросить"):
            continue
        rows.append({
            "Путь": str(record.get("Текущий путь", "")).replace("\\", "/"),
            "Имя": Path(str(record.get("Текущий путь", ""))).name,
            "Размер": 0,
            "Изменён": "",
            "Причина": str(record.get("_причина-выброса") or "модель сочла файл ненужным"),
            "Уверенность": "мнение ИИ",
            "Отметить": "FALSE",
        })
    return rows


def write_report(config: dict, rows: list[dict]) -> Path:
    from .scanner import write_csv

    fields = ["Путь", "Имя", "Размер", "Изменён", "Причина", "Уверенность", "Отметить"]
    target = config["output_path"] / "trash_candidates.csv"
    write_csv(target, fields, rows)
    return target


def move_to_trash(config: dict, rows: list[dict], progress=None) -> dict:
    """Переместить отмеченное в папку корзины. Удаления не происходит."""
    root: Path = config["scan_root"]
    output: Path = config["output_path"]
    trash = trash_dir(config)
    from .applier import _load_log, _safe_target, _save_log, batch_id, unique_path

    batch = batch_id()
    moved, failed, undo = 0, 0, []

    log_rows = _load_log(output / "operations_log.csv")
    for number, row in enumerate(rows, 1):
        rel = str(row.get("Путь", "")).replace("\\", "/").strip("/")
        if str(row.get("Отметить", "")).upper() != "TRUE" or not rel:
            continue
        if progress:
            progress(number, len(rows), rel)
        try:
            source = _safe_target(root, rel)   # «..» и чужие диски — сюда не проходят
        except ValueError as exc:
            failed += 1
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": "в корзину",
                "Исходный путь": rel,
                "Новый путь": "",
                "Статус": "ошибка: опасный путь",
                "Комментарий": str(exc)[:200],
            })
            continue
        if not source.exists():
            failed += 1
            continue
        try:
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / source.name
            target, _ = unique_path(target)
            shutil.move(str(source), str(target))
            if not target.exists() or source.exists():
                raise OSError("перемещение не подтвердилось")
            moved += 1
            undo.append({"from": str(target), "to": str(source)})
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": "в корзину",
                "Исходный путь": rel,
                "Новый путь": target.relative_to(root).as_posix() if target.is_relative_to(root) else str(target),
                "Статус": "ok",
                "Комментарий": f"пачка {batch}; {row.get('Причина', '')}"[:300],
            })
            _drop_from_map(output, rel)
        except Exception as exc:
            failed += 1
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": "в корзину",
                "Исходный путь": rel,
                "Новый путь": "",
                "Статус": f"ошибка: {type(exc).__name__}",
                "Комментарий": str(exc)[:200],
            })

    _save_log(output / "operations_log.csv", log_rows)
    if undo:
        (output / f"undo_{batch}.json").write_text(json.dumps(undo, ensure_ascii=False, indent=2), "utf-8")
    return {"пачка": batch, "перемещено": moved, "ошибок": failed, "корзина": str(trash)}


def _drop_from_map(output: Path, rel: str) -> None:
    """Строка корзины не должна остаться в активной карте переноса."""
    path = output / MAP_NAME
    if not path.exists():
        return
    rows = load_map(path)
    kept = [row for row in rows if (row.get("Текущий путь") or "").replace("\\", "/").strip("/") != rel]
    if len(kept) != len(rows):
        save_map(path, kept)

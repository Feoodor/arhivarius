"""Проверка предложений, которые пишет внешний разбор (Claude Code, вручную).

Модель не должна переносить файлы сама: она пишет suggestions-файл, а программа проверяет его
здесь, до слияния в карту. Ошибки блокируют слияние, предупреждения только показываются.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REQUIRED = ("Текущий путь", "Новое имя", "Целевой путь", "Тип операции",
            "Уверенность имени", "Уверенность переноса", "Причина", "Статус")
OPERATIONS = {"перенос + переименование", "перенос", "переименование", "в корзину", "без изменений"}
STATUSES = {"готово", "проверка"}
FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*')
DATE_PREFIX = re.compile(r"^(\d{4}(-\d{2}(-\d{2})?)?_)")
MIN_CONFIDENCE = 0.90


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def validate(records: list, snapshot: dict | None, config: dict) -> tuple[list[str], list[str]]:
    """Вернуть (ошибки, предупреждения). Пустой список ошибок = файл можно сливать."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(records, list):
        return ["файл должен содержать JSON-список записей"], warnings

    files = {}
    folders: set[str] = set()
    if snapshot:
        files = {_norm(f.get("path", "")): f for f in snapshot.get("files", [])}
        folders = {_norm(f) for f in snapshot.get("folders", [])}
    else:
        warnings.append("снимка нет: проверка существования файлов и папок пропущена (выполните scan)")
    top_roots = {_norm(x).split("/")[0] for x in config.get("top_folders", []) if str(x).strip()}
    top_roots |= {f.split("/")[0] for f in folders if f}

    seen_sources: Counter = Counter()
    seen_targets: Counter = Counter()
    for number, rec in enumerate(records, 1):
        where = f"запись {number}"
        if not isinstance(rec, dict):
            errors.append(f"{where}: не объект")
            continue
        missing = [key for key in REQUIRED if key not in rec]
        if missing:
            errors.append(f"{where}: нет полей {', '.join(missing)}")
            continue
        src = _norm(rec["Текущий путь"])
        where = f"{where} ({src})"
        seen_sources[src] += 1
        if snapshot and src not in files:
            errors.append(f"{where}: файла нет в снимке — выполните scan или проверьте путь")

        op = str(rec["Тип операции"]).strip()
        if op not in OPERATIONS:
            errors.append(f"{where}: «Тип операции» должен быть одним из: {', '.join(sorted(OPERATIONS))}")
        status = str(rec["Статус"]).strip()
        if status not in STATUSES:
            errors.append(f"{where}: «Статус» должен быть «готово» или «проверка»")

        name = str(rec["Новое имя"] or "").strip()
        target = _norm(rec["Целевой путь"])
        if op == "в корзину":
            if target:
                warnings.append(f"{where}: для «в корзину» целевая папка не нужна")
        else:
            if op in {"перенос", "перенос + переименование"} and not target:
                errors.append(f"{where}: для переноса не указана «Целевой путь»")
            if not name:
                errors.append(f"{where}: пустое «Новое имя»")
        if name and any(ch in FORBIDDEN_NAME_CHARS for ch in name):
            errors.append(f"{where}: в имени запрещённые символы: <>:\"/\\|?*")
        if name and " " in name:
            warnings.append(f"{where}: в имени есть пробелы — по регламенту слова через дефис, а блоки через _")
        if name and not DATE_PREFIX.match(name):
            warnings.append(f"{where}: имя без даты в начале (ГГГГ-ММ-ДД_ или ГГГГ-ММ_ или ГГГГ_)")
        if name and src and op != "в корзину":
            old_ext = Path(src).suffix.lower()
            if old_ext and not name.lower().endswith(old_ext):
                errors.append(f"{where}: расширение изменено ({old_ext} → {Path(name).suffix.lower() or 'нет'})")
        if target and snapshot and target not in folders:
            if not status == "проверка":
                errors.append(f"{where}: папки «{target}» нет в снимке — такая папка новая, статус должен быть «проверка»")
            else:
                warnings.append(f"{where}: новая папка «{target}» — решите вручную")
        if target and top_roots and target.split("/")[0] not in top_roots:
            warnings.append(f"{where}: корневого раздела «{target.split('/')[0]}» нет в списке папок отдела")
        if target and name:
            seen_targets[f"{target}/{name}"] += 1

        for field in ("Уверенность имени", "Уверенность переноса"):
            try:
                value = float(rec[field])
            except (TypeError, ValueError):
                errors.append(f"{where}: «{field}» должно быть числом от 0 до 1")
                continue
            if not 0 <= value <= 1:
                errors.append(f"{where}: «{field}» вне диапазона 0–1")
            elif value < MIN_CONFIDENCE and status == "готово":
                errors.append(f"{where}: «{field}» {value} ниже {MIN_CONFIDENCE}, статус должен быть «проверка»")

        reason = str(rec["Причина"] or "").strip()
        if len(reason) < 15:
            warnings.append(f"{where}: короткая причина — укажите факт из текста")

    for src, count in seen_sources.items():
        if count > 1:
            errors.append(f"«{src}» указан {count} раз(а) — одна строка на файл")
    for dst, count in seen_targets.items():
        if count > 1:
            errors.append(f"два файла хотят стать «{dst}»")
    return errors, warnings

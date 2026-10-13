"""Распаковка архивов: только то, что человек отметил. Модель и скан ничего не распаковывают.

Архив распаковывается в папку рядом с ним (имя без расширения). Если распаковка прошла, архив
уходит в корзину отдела — не удаляется, откатывается тем же журналом. Если не прошла, архив
остаётся на месте, а причина пишется в archive_status.csv и видна в окне. Распаковывать вручную
и удалять архив после этого — дело человека.
"""
from __future__ import annotations

import csv
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import cleanup
from .applier import _safe_target
from .scanner import load_snapshot

STATUS_NAME = "archive_status.csv"
STATUS_FIELDS = ["Путь", "Статус", "Комментарий", "Дата"]
SEVEN_ZIP_PATHS = ("C:/Program Files/7-Zip/7z.exe", "C:/Program Files (x86)/7-Zip/7z.exe")


def find_extractor() -> str | None:
    """7-Zip распаковывает zip, rar, 7z, tar и gz. Ищем в стандартных папках, потом в PATH."""
    for candidate in SEVEN_ZIP_PATHS:
        if Path(candidate).exists():
            return candidate
    return shutil.which("7z")


def archive_rows(config: dict) -> list[dict]:
    """Архивы из снимка по расширениям из настроек. Ничего не читает с диска, кроме снимка."""
    snapshot = load_snapshot(config) or {"files": []}
    exts = {str(ext).lower() for ext in config.get("archive_extensions", [])}
    rows = []
    for item in snapshot.get("files", []):
        rel = str(item.get("path", "")).replace("\\", "/")
        if str(item.get("extension", "")).lower() not in exts:
            continue
        rows.append({"Путь": rel, "Имя": Path(rel).name, "Размер": int(item.get("size") or 0),
                     "Отметить": "FALSE"})
    rows.sort(key=lambda row: row["Путь"].casefold())
    return rows


def load_status(config: dict) -> dict[str, dict]:
    path = config["output_path"] / STATUS_NAME
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["Путь"]: row for row in csv.DictReader(handle, delimiter=";")}


def _save_status(config: dict, status: dict[str, dict]) -> None:
    path = config["output_path"] / STATUS_NAME
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATUS_FIELDS, delimiter=";")
        writer.writeheader()
        for rel in sorted(status):
            writer.writerow({field: status[rel].get(field, "") for field in STATUS_FIELDS})


def _fail(status: dict, rel: str, text: str) -> None:
    status[rel] = {"Путь": rel, "Статус": "ошибка распаковки", "Комментарий": text[:300],
                   "Дата": datetime.now().isoformat(timespec="seconds")}


def unpack(config: dict, rels: list[str], progress=None) -> dict:
    """Распаковать отмеченные архивы. Успешные уходят в корзину, неудачные остаются с причиной."""
    extractor = find_extractor()
    if extractor is None:
        return {"ошибка": "не найден 7-Zip (7z.exe): установите 7-Zip и повторите"}
    root: Path = config["scan_root"]
    status = load_status(config)
    to_trash: list[dict] = []
    done = failed = 0
    for number, raw in enumerate(rels, 1):
        rel = raw.replace("\\", "/").strip("/")
        if progress:
            progress(number, len(rels), rel)
        try:
            source = _safe_target(root, rel)
        except ValueError as exc:
            failed += 1
            _fail(status, rel, f"опасный путь: {exc}")
            continue
        if not source.is_file():
            failed += 1
            _fail(status, rel, "файла уже нет на месте")
            continue
        target = source.parent / source.stem
        if target.exists():
            failed += 1
            _fail(status, rel, f"папка «{target.name}» уже есть — распакуйте вручную и удалите архив")
            continue
        target.mkdir(parents=True)
        result = subprocess.run([extractor, "x", "-y", "-bso0", "-bsp0", f"-o{target}", str(source)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        extracted = any(target.rglob("*"))
        if result.returncode == 0 and extracted:
            done += 1
            status.pop(rel, None)
            to_trash.append({"Путь": rel, "Отметить": "TRUE", "Причина": f"распакован в папку {target.name}"})
            continue
        failed += 1
        reason = (result.stderr or result.stdout or "").strip().splitlines()
        text = reason[-1] if reason else f"код ошибки {result.returncode}"
        if not extracted:
            text += " (архив пуст или не открылся)"
        else:
            text += f"; частично распакован в «{target.name}» — проверьте и удалите эту папку вручную"
        _fail(status, rel, text)
    _save_status(config, status)
    moved = cleanup.move_to_trash(config, to_trash)["перемещено"] if to_trash else 0
    return {"распаковано": done, "с ошибкой": failed, "архивов в корзине": moved}

"""Строительные леса тестов: временный «архив отдела» и конфиг под поддельную Ollama."""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import scanner
from core.config_loader import load_config
from core.folders import FolderIndex

FOLDERS = [
    "Документы/00_Разобрать",
    "Документы/Входящие/2026",
    "Документы/Исходящие/2026",
    "Документы/Прошения - Рапорты/2026",
    "Документы/Отчеты и планы по работе/2026",
    "Документы/Мероприятия/Слеты/2026/2026-07-17_Летний-слет",
    "Документы/Мероприятия/Слеты/2026/2026-07-17_Летний-слет/06_Участники",
    "Документы/Шаблоны",
    "Документы/Дизайн/Скриншоты",
]

FILES = {
    # имя: текст
    "scan001.txt": "Входящее письмо Вх-123 от 12.03.2026. Администрация города приглашает отдел на встречу 20 марта 2026.",
    "doc2.txt": "РАПОРТ. Прошу благословить проведение интеллектуальной игры 9 мая 2026 года. Подпись: руководитель отдела.",
    "notes.txt": "Список участников летнего слёта 17 июля 2026 с телефонами: Иванов 555-01-02, Петров 555-03-04.",
}


def make_archive(tmp: Path, files: dict[str, str] | None = None, extra_llm: dict | None = None):
    """Создаёт архив и конфиг. Возвращает (config, root)."""
    root = tmp / "archive"
    for folder in FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    for name, text in (FILES if files is None else files).items():
        (root / "Документы/00_Разобрать" / name).write_text(text, encoding="utf-8")
    app_dir = tmp / "app"
    app_dir.mkdir(exist_ok=True)
    raw = {"mo_root": str(root), "scan_root": str(root), "output_dir": str(app_dir / "_output"),
           "llm": {"retries": 1, "cache_policy": "none", "timeout_seconds": 20,
                   "num_ctx": 16384, "content_chars": 3000, **(extra_llm or {})},
           "folders": {"hierarchical": False},
           "queue": {"rules_first": True, "mode": "завалы"}}
    (app_dir / "config.json").write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    return load_config(app_dir), root


def scan_and_index(config: dict):
    snapshot = scanner.scan(config)
    index = FolderIndex.from_snapshot(
        snapshot, prune=[*sorted(config["excluded_dirs"]), config["output_path"].name],
        exclude=config["folders"].get("hide_prefixes", []))
    return snapshot, index


_NAME = re.compile(r"^Имя:\s*(.+)$", re.M)


def department_responder(payload: dict) -> dict:
    """«Модель», которая рассуждает по ключевым словам в файле — как разумный человек."""
    user = payload["messages"][-1]["content"]
    name = (_NAME.search(user) or [None, "file.txt"])[1].strip()
    ext = Path(name).suffix
    low = user.split("СОДЕРЖИМОЕ:", 1)[-1].lower()      # смотрим только текст файла, не карту папок
    base = {"doc_type": "", "direction": "", "reg_number": "", "doc_date": "", "event_name": "",
            "event_start": "", "event_end": "", "shortcut_to": "", "name_confidence": 0.95,
            "move_confidence": 0.95, "confidence": 0, "review": False, "discard": False,
            "discard_reason": "", "reason": "по тексту"}
    if "вх-123" in low:
        return {**base, "doc_type": "Письмо", "direction": "вх", "reg_number": "123", "doc_date": "2026-03-12",
                "new_name": f"2026-03-12_Вх-123_Письмо_Приглашение-на-встречу_Администрация{ext}",
                "target_path": "Документы/Входящие/2026"}
    if "рапорт" in low:
        return {**base, "doc_type": "Рапорт", "doc_date": "2026-05-09",
                "new_name": f"2026-05-09_Рапорт_Игра-9-мая{ext}",
                "target_path": "Документы/Прошения - Рапорты/2026"}
    if "слёт" in low or "слет" in low:
        return {**base, "doc_type": "Список", "event_name": "Летний слёт", "event_start": "2026-07-17",
                "doc_date": "2026-07-17", "review": True, "name_confidence": 0.82, "move_confidence": 0.75,
                "new_name": f"2026-07-17_Список_Участники-слета{ext}",
                "target_path": "Документы/Мероприятия/Слеты/2026/2026-07-17_Летний-слет/06_Участники"}
    return {**base, "new_name": name, "target_path": "Документы/00_Разобрать", "review": True,
            "name_confidence": 0.3, "move_confidence": 0.3}

"""Проход «папка целиком»: модель видит ВСЕ имена файлов сразу и согласует их друг с другом.

Обычный режим подаёт модели по одному файлу — и изображения без содержимого
угадываются вслепую. Здесь в один запрос кладётся полный список файлов папки
(без подпапок): так модель видит шаблоны названий, пары «афиша + фото» и может
предложить внятные имена даже тому, что не читается. Изображения дополнительно
проходят зрение (по желанию). Результат — строки карты со статусом «проверка»:
ни одно решение не применяется автоматически.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path

from . import mapio
from .config_loader import MAP_NAME
from .extract import IMAGE_EXT, extract
from .prompts import build_system, parse_answer
from .rules import conf_value

BATCH_ITEM = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "имя файла точно как в списке"},
        "new_name": {"type": "string", "description": "имя по регламенту, расширение не менять"},
        "target_path": {"type": "string", "description": "папка из карты, без имени файла"},
        "confidence": {"type": "number", "description": "0..1"},
        "reason": {"type": "string", "description": "1–2 фразы: на основании чего"},
    },
    "required": ["file", "new_name", "target_path", "confidence", "reason"],
}

BATCH_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": BATCH_ITEM}},
    "required": ["items"],
}

_VISION_ITEM = {
    "type": "object",
    "properties": {
        "what": {"type": "string", "description": "что изображено, 1 фраза"},
        "new_name": {"type": "string", "description": "имя файла по регламенту (тип, дата/событие если видны)"},
        "target_path": {"type": "string", "description": "папка из карты"},
        "confidence": {"type": "number"},
    },
    "required": ["what", "new_name", "target_path", "confidence"],
}


def list_folder(config: dict, rel_folder: str) -> list[Path]:
    root: Path = config["scan_root"]
    folder = (root / rel_folder.replace("\\", "/").strip("/")).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"папка не найдена: {folder}")
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and not p.name.startswith(("~$", ".", "desktop.ini")))


def _samples(files: list[Path], budget_chars: int = 5200, per_file: int = 420) -> str:
    """Короткие текстовые выжимки читаемых файлов — для контекста именования."""
    out: list[str] = []
    used = 0
    for path in files:
        if path.suffix.lower() in IMAGE_EXT or used >= budget_chars:
            continue
        try:
            data = extract(path, per_file, 1)
        except Exception:
            continue
        text = " ".join((data.text or "").split())[:per_file]
        if text:
            out.append(f"  · {path.name}: {text}")
            used += len(text)
    return "\n".join(out)


def folder_pass(config: dict, rel_folder: str, limit: int = 60, vision: bool = False,
                log=None, progress=None) -> dict:
    """Один запрос на всю папку (+ зрение по одному на картинку). Возвращает сводку."""
    log = log or (lambda msg: None)
    from .llm import OllamaClient
    from .pipeline import _snapshot_and_index

    _, index = _snapshot_and_index(config)
    folders_cfg = config.get("folders", {})
    pack = index.build(
        anchor=rel_folder.replace("\\", "/").strip("/"),
        keywords=[],
        skeleton_depth=int(folders_cfg.get("depth", 5)),
        anchor_depth=2,
        max_paths=int(folders_cfg.get("max_paths", 300)),
        budget_chars=int(folders_cfg.get("budget_chars", 7000)),
        show_files=bool(folders_cfg.get("show_file_counts", True)),
    )

    files = list_folder(config, rel_folder)[:max(1, int(limit))]
    if not files:
        return {"файлов": 0}

    def _kb(p: Path) -> int:
        try:
            return p.stat().st_size // 1024
        except OSError:
            return 0

    names = "\n".join(f"- {p.name} ({_kb(p)} КБ)" for p in files)
    llm_cfg = config.get("llm", {})
    client = OllamaClient(
        base_url=llm_cfg.get("base_url", "http://127.0.0.1:11434"),
        model=llm_cfg.get("model_text", "gemma3:12b"),
        num_ctx=int(llm_cfg.get("num_ctx", 16384)),
        num_predict=2600,
        temperature=0.15,
        keep_alive=llm_cfg.get("keep_alive", "30m"),
        timeout=int(llm_cfg.get("timeout_seconds", 900)),
        cache_policy="none",
        structured=True,
        retries=1,
    )

    system = build_system(config) + (
        "\n\nРЕЖИМ «ПАПКА ЦЕЛИКОМ»: тебе показывают ВСЮ папку сразу. Сравни файлы между "
        "собой: серии, пары (афиша+фото, протокол+список), общие даты и события. Имена "
        "должны различать содержание, а не «фото1, фото2». Для картинок без подсказок в "
        "имени используй соседей по папке и названия самой папки как контекст события. "
        "Ответь строго JSON вида {\"items\": [...]} — по одному объекту на каждый файл."
    )
    user = (
        f"ПАПКА: {rel_folder}\n\nСУЩЕСТВУЮЩИЕ ПАПКИ (карта):\n{pack.text}\n\n"
        f"ФАЙЛЫ ({len(files)}):\n{names}\n\nВЫЖИМКИ СОДЕРЖИМОГО:\n{_samples(files) or '(текстовых файлов нет)'}\n\n"
        "Для каждого файла предложи new_name и target_path. Расширения не меняй."
    )
    log(f"папка «{rel_folder}»: один запрос на {len(files)} файлов…")
    result = client.ask(system, user, schema=BATCH_SCHEMA)
    items = (result.data or {}).get("items") or []
    try:
        known = {p.name: p for p in files}
    except Exception:
        known = {}

    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file", "")).strip().strip('"')
        path = known.get(name)
        if path is None:
            for known_name, candidate in known.items():
                if known_name.casefold() == name.casefold():
                    path = candidate
                    break
        if path is None or path.name.casefold() in seen:
            continue
        seen.add(path.name.casefold())
        rel = str(path.relative_to(config["scan_root"])).replace("\\", "/")
        new_name = _keep_ext(path, str(item.get("new_name", "") or path.name))
        conf = conf_value(item.get("confidence")) or 0.0
        rows.append({
            "Текущий путь": rel, "Текущее имя": path.name,
            "Тип операции": "перенос + переименование" if (new_name != path.name or
                            str(item.get("target_path", "")).strip("/") != rel.rpartition("/")[0]) else "без изменений",
            "Новое имя": new_name,
            "Целевой путь": str(item.get("target_path", "")).strip().strip("/"),
            "Путь ярлыка": "",
            "Уверенность имени": f"{conf:.2f}",
            "Уверенность переноса": f"{conf:.2f}",
            "Причина": "проход «папка целиком»: " + str(item.get("reason", ""))[:220],
            "Применить": "FALSE", "Статус": "проверка", "Комментарий": "",
        })

    if vision:
        rows += _vision_pass(config, [p for p in files if p.suffix.lower() in IMAGE_EXT
                                      and p.name.casefold() not in {r["Текущее имя"].casefold() for r in rows}],
                            pack.text, log, progress)

    merged = _merge_map(config, rows)
    return {"файлов": len(files), "предложений": len(rows),
            "новых_строк_в_карте": merged["новых"], "обновлено": merged["обновлено"]}


def _vision_pass(config: dict, images: list[Path], folders_block: str, log, progress) -> list[Path | dict]:
    """Картинки по одной через зрение модели; возвращает строки карты."""
    from .llm import OllamaClient

    llm_cfg = config.get("llm", {})
    client = OllamaClient(
        base_url=llm_cfg.get("base_url", "http://127.0.0.1:11434"),
        model=llm_cfg.get("model_vision", llm_cfg.get("model_text", "gemma3:12b")),
        num_ctx=int(llm_cfg.get("num_ctx", 16384)),
        num_predict=700, temperature=0.1,
        keep_alive=llm_cfg.get("keep_alive", "30m"),
        timeout=int(llm_cfg.get("timeout_seconds", 900)),
        cache_policy="none", structured=True, retries=1,
    )
    system = build_system(config) + ("\n\nРежим «зрение»: смотришь картинку. new_name — по регламенту, "
                                     "по тому, что видно (баннер события, надпись, люди, храм). Не выдумывай даты.")
    rows = []
    for number, path in enumerate(images, 1):
        if progress:
            progress(number, len(images), path.name)
        try:
            b64 = base64.b64encode(path.read_bytes()).decode()
            res = client.ask(system, f"СУЩЕСТВУЮЩИЕ ПАПКИ:\n{folders_block}\n\nФайл: {path.name}",
                             schema=_VISION_ITEM, images=[b64])
            data = res.data or {}
        except Exception as exc:
            log(f"зрение не сработало для {path.name}: {type(exc).__name__}")
            continue
        rel = str(path.relative_to(config["scan_root"])).replace("\\", "/")
        conf = conf_value(data.get("confidence")) or 0.0
        rows.append({
            "Текущий путь": rel, "Текущее имя": path.name,
            "Тип операции": "перенос + переименование",
            "Новое имя": _keep_ext(path, str(data.get("new_name", "") or path.name)),
            "Целевой путь": str(data.get("target_path", "")).strip().strip("/"),
            "Путь ярлыка": "",
            "Уверенность имени": f"{conf:.2f}",
            "Уверенность переноса": f"{conf:.2f}",
            "Причина": "зрение: " + str(data.get("what", ""))[:200],
            "Применить": "FALSE", "Статус": "проверка", "Комментарий": "",
        })
    return rows


def _keep_ext(path: Path, new_name: str) -> str:
    new_name = Path(new_name.strip() or path.name).name
    if not new_name.lower().endswith(path.suffix.lower()):
        new_name = (Path(new_name).stem or path.stem) + path.suffix
    return new_name


def _merge_map(config: dict, rows: list[dict]) -> dict:
    """Добавить/обновить строки в активной карте; существующие ручные правки не затираем."""
    target = config["output_path"] / MAP_NAME
    existing = mapio.load_map(target)
    by_path = {str(r.get("Текущий путь", "")).replace("\\", "/"): r for r in existing}
    added = updated = 0
    for row in rows:
        rel = row["Текущий путь"]
        old = by_path.get(rel)
        if old is None:
            existing.append(row)
            by_path[rel] = row
            added += 1
            continue
        if (str(old.get("Комментарий", "")).strip()
                or str(old.get("Путь ярлыка", "")).strip()
                or str(old.get("Применить", "")).strip().upper() == "TRUE"
                or str(old.get("Статус", "")) in {"готово", "перенесено"}):
            continue  # строку уже трогал человек — не переезжаем
        old.update(row)
        updated += 1
    mapio.save_map(target, existing)
    return {"новых": added, "обновлено": updated}

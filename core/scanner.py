"""Снимок диска: обход, хеши, инвентарь.

Только чтение — ни перемещений, ни удалений. Обход повторяемый: если файл не
менялся (путь + размер + время модификации), хеш берётся из прежнего снимка, и
повторный скан большой папки занимает секунды вместо часов.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config_loader import MAP_NAME, SNAPSHOT_NAME

INVENTORY_FIELDS = ["path", "name", "extension", "size", "modified", "sha256", "is_archive", "error"]
CHUNK = 1024 * 1024


def relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def is_excluded(path: Path, config: dict, is_dir: bool = False) -> bool:
    name = path.name.casefold()
    if name in config["excluded_names"]:
        return True
    if is_dir:
        if name in config["excluded_dirs"]:
            return True
        output = config.get("output_path")
        if output and name == Path(str(output)).name.casefold():
            return True   # служебная папка программы — не часть архива
    return any(name.startswith(prefix.casefold()) for prefix in config["excluded_prefixes"])


def _previous_index(config: dict) -> dict[str, dict]:
    """Прошлый снимок — чтобы не хешировать заново неизменённые файлы."""
    target = config["output_path"] / SNAPSHOT_NAME
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text("utf-8"))
    except Exception:
        return {}
    index = {}
    for item in data.get("files", []) + data.get("archives", []):
        key = str(item.get("path", "")).replace("\\", "/")
        index[key] = item
    return index


def scan(config: dict, progress: Callable[[str, int], None] | None = None) -> dict:
    root: Path = config["scan_root"]
    output: Path = config["output_path"]
    if not root.is_dir():
        raise FileNotFoundError(f"папка не найдена: {root}")
    output.mkdir(parents=True, exist_ok=True)
    previous = _previous_index(config)

    files: list[dict] = []
    archives: list[dict] = []
    folders: list[str] = []
    walk_errors: list[str] = []
    reused = hashed = 0
    position = 0

    def _note_error(err: OSError) -> None:
        if len(walk_errors) < 100:
            walk_errors.append(str(err))

    for current, dirs, names in os.walk(root, onerror=_note_error):
        current_path = Path(current)
        dirs[:] = sorted(d for d in dirs if not is_excluded(current_path / d, config, is_dir=True))
        cur_rel = os.path.relpath(current, root).replace("\\", "/")
        cur_rel = "" if cur_rel == "." else cur_rel
        if current_path != root:
            folders.append(cur_rel)
        for name in sorted(names):
            if is_excluded(current_path / name, config):
                continue
            path = current_path / name
            position += 1
            if progress and position % 200 == 0:
                progress(name, position)
            rel = f"{cur_rel + '/' if cur_rel else ''}{name}"
            try:
                stat = path.stat()
                row = {
                    "path": rel,
                    "name": name,
                    "extension": path.suffix.lower(),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                    "sha256": "",
                    "is_archive": name.lower().endswith(tuple(config["archive_extensions"])),
                    "error": "",
                }
                old = previous.get(rel)
                if old and old.get("size") == row["size"] and str(old.get("modified", ""))[:19] == row["modified"][:19] and old.get("sha256"):
                    row["sha256"] = old["sha256"]
                    reused += 1
                else:
                    row["sha256"] = sha256(path)
                    hashed += 1
            except OSError as exc:
                row = {
                    "path": rel, "name": name, "extension": path.suffix.lower(), "size": 0,
                    "modified": "", "sha256": "", "is_archive": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            files.append(row)
            if row["is_archive"]:
                archives.append(row)

    snapshot = {
        "root": str(root),
        "mo_root": str(config["mo_root"]),
        "created": datetime.now().isoformat(timespec="seconds"),
        "folders": sorted(set(folders)),
        "files": files,
        "archives": archives,
        "stats": {"хешей_взято_из_прошлого_снимка": reused, "захешировано_заново": hashed,
                  "ошибки_обхода": walk_errors},
    }
    target = output / SNAPSHOT_NAME
    tmp = output / (SNAPSHOT_NAME + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), "utf-8")
    tmp.replace(target)
    write_csv(output / "inventory.csv", INVENTORY_FIELDS, files)
    write_csv(output / "archives.csv", INVENTORY_FIELDS, archives)
    try:  # карта папок и индексы должны увидеть свежее дерево сразу, без перезапуска
        from . import pipeline
        pipeline.invalidate_snapshot_cache(output)
    except Exception:
        pass
    return snapshot


def load_snapshot(config: dict) -> dict | None:
    target = config["output_path"] / SNAPSHOT_NAME
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None   # битый снимок = «снимка нет»: потребитель честно попросит скан


def duplicate_groups(snapshot: dict) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for item in snapshot.get("files", []):
        digest = str(item.get("sha256") or "")
        if digest and int(item.get("size") or 0) > 0:
            groups.setdefault(digest, []).append(item)
    return sorted(((d, items) for d, items in groups.items() if len(items) > 1), key=lambda pair: -len(pair[1]))


def duplicates_report(config: dict) -> dict:
    snapshot = load_snapshot(config) or scan(config)
    groups = duplicate_groups(snapshot)
    rows = []
    for digest, items in groups:
        ordered = sorted(items, key=lambda i: (str(i.get("path", "")).count("/"), str(i.get("path", ""))))
        for number, item in enumerate(ordered, 1):
            rows.append({
                "хеш": digest[:12],
                "копия": number,
                "путь": item["path"],
                "размер": item["size"],
                "изменён": item["modified"],
                "пометка": "канон (самая короткая папка)" if number == 1 else "совпадение по содержимому",
            })
    output: Path = config["output_path"]
    write_csv(output / "duplicates.csv", ["хеш", "копия", "путь", "размер", "изменён", "пометка"], rows)
    wasted = sum(
        int(items[0]["size"]) * (len(items) - 1) for _, items in groups
    )
    return {"групп": len(groups), "лишних_копий": len(rows) - len(groups), "байт_на_повторах": wasted,
            "файл": str(output / "duplicates.csv")}


def summary(config: dict) -> dict:
    snapshot = load_snapshot(config)
    if not snapshot:
        return {"готов": False}
    return {
        "готов": True,
        "когда": snapshot.get("created", ""),
        "папок": len(snapshot.get("folders", [])),
        "файлов": len(snapshot.get("files", [])),
        "архивов": len(snapshot.get("archives", [])),
        "байт": sum(int(item.get("size") or 0) for item in snapshot.get("files", [])),
        "карта": (config["output_path"] / MAP_NAME).exists(),
    }

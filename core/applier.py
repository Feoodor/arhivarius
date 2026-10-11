"""Перенос и переименование по карте. Удалений нет ни в одном сценарии.

Правила безопасности:
  * трогается только то, что оператор отметил `Применить=TRUE`;
  * целевая папка создаётся, но никогда не затирается и не чистится;
  * при совпадении имён файл получает суффикс `_v2`, `_v3`, а не перезапись;
  * каждый шаг пишется в operations_log.csv и в журнал отката undo_<пачка>.json;
  * путь проверяется на «не выходит за пределы рабочего каталога» — `..` и уходы
    на другой диск отклоняются.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config_loader import MAP_NAME
from .mapio import APPLIED_NAME, MAP_FIELDS, archive_applied, load_map, save_map

LOG_FIELDS = ["Дата", "Действие", "Исходный путь", "Новый путь", "Статус", "Комментарий"]
APPLIED = "перенесено"
SKIPPED = "пропуск"
WINDOWS = os.name == "nt"
MAX_PATH = 259          # длиннее проводник Windows и shutil.move без «длинных путей» не осилят
RESERVED_NAMES = {  # Windows не создаст файл с таким именем ни в одной папке
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


@dataclass
class Action:
    файл: str
    тип: str
    источник: str
    приём: str
    возможно: bool
    заметка: str = ""


def _safe_target(root: Path, rel: str) -> Path:
    """Относительный путь → абсолютный, с проверкой, что мы не ушли из корня."""
    rel = (rel or "").replace("\\", "/").strip("/")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"путь вне рабочего каталога: {rel}") from exc
    return candidate


def unique_path(path: Path) -> tuple[Path, bool]:
    if not path.exists():
        return path, False
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    for number in range(2, 1000):
        candidate = parent / f"{stem}_v{number}{suffix}"
        if not candidate.exists():
            return candidate, True
    return parent / f"{stem}_v{datetime.now():%H%M%S}{suffix}", True


def build_plan(config: dict, rows: list[dict], only_marked: bool = True) -> list[Action]:
    root: Path = config["scan_root"]
    plan: list[Action] = []
    seen: set[str] = set()
    for row in rows:
        rel = (row.get("Текущий путь") or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        if (row.get("Применить") or "").strip().upper() != "TRUE" and only_marked:
            continue
        if rel in seen:
            continue
        seen.add(rel)
        status = (row.get("Статус") or "").strip()
        if status == APPLIED:
            plan.append(Action(rel, "уже перенесён", rel, "", False, "строка закрыта ранее"))
            continue
        target_dir = (row.get("Целевой путь") or "").replace("\\", "/").strip("/")
        new_name = re.sub(r'[<>:"/\\|?*]', "", (row.get("Новое имя") or "").strip())
        new_name = new_name.rstrip(" .")   # Windows молча срезает точку/пробел на конце
        target_dir = "/".join(s.strip().rstrip(" .") for s in target_dir.split("/") if s.strip(" ."))
        if not new_name:
            plan.append(Action(rel, "неполная строка", rel, "", False, "нет нового имени"))
            continue
        if new_name.split(".")[0].casefold() in RESERVED_NAMES:
            plan.append(Action(rel, "неполная строка", rel, "", False,
                               f"имя «{new_name}» зарезервировано Windows (con, prn, lpt…)"))
            continue
        if not target_dir:
            target_dir = rel.rpartition("/")[0]   # пустая цель = переименовать на месте (без переноса)
        if not target_dir:
            plan.append(Action(rel, "неполная строка", rel, "", False, "нет целевой папки и файл лежит в корне"))
            continue
        try:
            src = _safe_target(root, rel)
            dst = _safe_target(root, f"{target_dir}/{new_name}")
        except ValueError as exc:
            plan.append(Action(rel, "опасный путь", rel, "", False, str(exc)))
            continue
        if not src.exists():
            plan.append(Action(rel, "нет файла", rel, "", False, "файл не найден — возможно, уже перенесён вручную"))
            continue
        if WINDOWS and len(str(dst)) > MAX_PATH:
            plan.append(Action(rel, "слишком длинный путь", rel, "", False,
                               f"путь длиннее {MAX_PATH} символов — сократите имя или вложенность папок"))
            continue
        final, renamed = unique_path(dst)
        same = src.resolve() == final.resolve()
        if (src.parent == dst.parent and src.name != dst.name
                and src.name.casefold() == dst.name.casefold()):
            final, renamed = dst, False    # осмысленная правка регистра — без суффикса
            same = False
        тип = "переместить"
        if src.parent != final.parent and src.name != final.name:
            тип = "переместить + переименовать"
        elif src.name != final.name:
            тип = "переименовать"
        note = "файл с таким именем уже есть → будет сохранён как %s" % final.name if renamed else ""
        plan.append(Action(rel, тип, str(src), str(final), not same, note))
    return [action for action in plan if action.возможно]


def describe(plan: list[Action], config: dict) -> tuple[list[str], dict]:
    root: Path = config["scan_root"]
    lines: list[str] = []
    counters = {"всего": len(plan), "новых папок": 0, "конфликтов": 0, "байт": 0}
    new_dirs: set[str] = set()
    for action in plan:
        try:
            src_rel = Path(action.источник).resolve().relative_to(root.resolve()).as_posix()
            dst_rel = Path(action.приём).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            src_rel, dst_rel = action.файл, action.приём
        lines.append(f"{action.тип}: {src_rel}  →  {dst_rel}" + (f"   [{action.заметка}]" if action.заметка else ""))
        parent = str(Path(action.приём).parent)
        if not Path(parent).exists():
            new_dirs.add(dst_rel.rpartition("/")[0])
        if "_v" in Path(dst_rel).stem[-4:]:
            counters["конфликтов"] += 1
        try:
            counters["байт"] += Path(action.источник).stat().st_size
        except OSError:
            pass
    counters["новых папок"] = len(new_dirs)
    if new_dirs:
        lines.insert(0, "Будут созданы папки:\n" + "\n".join(f"  + {d}" for d in sorted(new_dirs)))
    return lines, counters


def batch_id() -> str:
    """Метка пачки с микросекундами: два журнала за одну секунду не перетирают друг друга."""
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def merge_back(output: Path, touched: list[dict]) -> None:
    """Обновить полную карту на диске, а не перезаписать её переданным срезом."""
    path = output / MAP_NAME
    full = load_map(path)
    by_key = {(row.get("Текущий путь") or "").replace("\\", "/").strip("/"): row for row in full}
    fields = ("Статус", "Применить", "Целевой путь", "Новое имя", "Тип операции", "Комментарий", "Путь ярлыка")
    for row in touched:
        key = (row.get("Текущий путь") or "").replace("\\", "/").strip("/")
        if not key:
            continue
        target = by_key.get(key)
        if target is None:
            fresh = {field: row.get(field, "") for field in MAP_FIELDS}
            full.append(fresh)
            by_key[key] = fresh
            continue
        for field in fields:
            value = row.get(field)
            if value not in (None, ""):
                target[field] = str(value)
    save_map(path, full)


def apply_plan(
    config: dict,
    rows: list[dict],
    only_marked: bool = True,
    progress=None,
    make_shortcuts: bool = True,
) -> dict:
    """Выполнить план. Возвращает сводку; карта обновляется на месте."""
    root: Path = config["scan_root"]
    output: Path = config["output_path"]
    output.mkdir(parents=True, exist_ok=True)
    plan = build_plan(config, rows, only_marked)
    batch = batch_id()
    undo: list[dict] = []
    moved = failed = 0
    taken: set[str] = set()
    undo_journal = None
    log_rows = _load_log(output / "operations_log.csv")

    def _journal(new_steps: list[dict]) -> None:
        """Дописываемый журнал отката: сбой процесса в середине пачки не оставляет
        уже перемещённые файлы без записи."""
        nonlocal undo_journal
        if not new_steps:
            return
        try:
            if undo_journal is None:
                undo_journal = (output / f"undo_{batch}.jsonl").open("a", encoding="utf-8")
            for step in new_steps:
                undo_journal.write(json.dumps(step, ensure_ascii=False) + "\n")
            undo_journal.flush()
            os.fsync(undo_journal.fileno())
        except OSError:
            pass    # пошаговый журнал — страховка; итоговый undo_*.json всё равно будет записан в конце

    for position, action in enumerate(plan, 1):
        if progress:
            progress(position, len(plan), action.файл)
        try:
            src = Path(action.источник)
            dst = Path(action.приём)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.parent.is_dir():
                raise OSError(f"не удалось подготовить папку {dst.parent}")
            case_only = (src.parent == dst.parent and src.name != dst.name
                         and src.name.casefold() == dst.name.casefold())
            # переуваем цель ЗАНОВО: с момента плана её мог занять сосед по пачке,
            # DriveFS или папка с таким же именем — иначе shutil.move затрёт чужое
            if not case_only and (dst.exists() or str(dst).casefold() in taken):
                dst, _ = unique_path(dst)
                if dst.exists() or str(dst).casefold() == str(src).casefold():
                    raise OSError("цель занята и свободных имён нет — отметьте перенос заново")
            taken.add(str(dst).casefold())
            before = len(undo)
            shutil.move(str(src), str(dst))
            if not dst.exists():
                raise OSError("перемещение не подтвердилось")
            if not case_only and src.exists():
                raise OSError("источник жив после переноса — проверьте доступ к диску")
            undo.append({"from": str(dst), "to": str(src)})
            moved += 1
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": action.тип,
                "Исходный путь": str(src.relative_to(root) if src.is_relative_to(root) else src),
                "Новый путь": str(dst.relative_to(root) if dst.is_relative_to(root) else dst),
                "Статус": "ok",
                "Комментарий": f"пачка {batch}" + (f"; {action.заметка}" if action.заметка else ""),
            })
            _mark_row(rows, action.файл, APPLIED, dst, make_shortcuts, root, config, log_rows, batch, undo)
            _journal(undo[before:])
        except Exception as exc:
            failed += 1
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": action.тип,
                "Исходный путь": action.источник,
                "Новый путь": action.приём,
                "Статус": f"ошибка: {type(exc).__name__}",
                "Комментарий": str(exc)[:200],
            })
            _mark_row(rows, action.файл, "ошибка", None, False, root, config, log_rows, batch, undo)

    if undo_journal is not None:
        undo_journal.close()
    _save_log(output / "operations_log.csv", log_rows)
    if undo:
        tmp = output / f"undo_{batch}.json.tmp"
        tmp.write_text(json.dumps(undo, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(output / f"undo_{batch}.json")
        try:  # пошаговый журнал больше не нужен — помечаем, чтобы undo его не читал
            (output / f"undo_{batch}.jsonl").rename(output / f"undo_{batch}.jsonl.done")
        except OSError:
            pass
    merge_back(output, rows)
    archived = archive_applied(output)
    return {"пачка": batch, "запланировано": len(plan), "перенесено": moved, "ошибок": failed,
            "в_историю": archived, "журнал": str(output / "operations_log.csv")}


def _mark_row(rows, rel, status, dst, make_shortcut, root, config, log_rows, batch, undo=None) -> None:
    for row in rows:
        if (row.get("Текущий путь") or "").replace("\\", "/").strip("/") != rel:
            continue
        row["Статус"] = status
        if status == APPLIED:
            row["Применить"] = "FALSE"
            shortcut = (row.get("Путь ярлыка") or "").strip()
            if make_shortcut and shortcut and dst:
                created = create_shortcut(root, shortcut, Path(dst), config)
                if created:
                    log_rows.append({
                        "Дата": datetime.now().isoformat(timespec="seconds"),
                        "Действие": "ярлык",
                        "Исходный путь": str(Path(dst).relative_to(root) if Path(dst).is_relative_to(root) else dst),
                        "Новый путь": created,
                        "Статус": "ok",
                        "Комментарий": f"пачка {batch}; папка ярлыка: {shortcut}",
                    })
                    if undo is not None:
                        undo.append({"создан": created})
        return


def create_shortcut(root: Path, rel_dir: str, source_abs: Path, config: dict) -> str:
    """Ярлык на канонический файл в папке Входящие/Исходящие.

    Сначала настоящий .lnk (так привычнее проводнику), если Windows Script Host
    недоступен — .url (тоже открывается двойным щелчком), и лишь затем текстовый
    указатель. Ничего не затирается: при конфликте добавляется _v2.
    """
    try:
        folder = _safe_target(root, rel_dir)
    except ValueError:
        return ""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    stem = Path(source_abs).stem
    target = folder / f"{stem}.lnk"
    if target.exists():
        target, _ = unique_path(target)
    try:
        import pythoncom  # noqa: F401
        import win32com.client

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            link = shell.CreateShortcut(str(target))
            link.TargetPath = str(source_abs)
            link.WorkingDirectory = str(source_abs.parent)
            link.Description = "Ярлык создан Архивариусом МО: канон лежит здесь"
            link.Save()
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        if target.exists():
            return str(target)
    except Exception:
        pass
    target = folder / f"{stem}.url"
    if target.exists():
        target, _ = unique_path(target)
    try:
        target.write_text(
            "[InternetShortcut]\r\nURL=file:///" + str(source_abs).replace("\\", "/").replace(" ", "%20") + "\r\n",
            "utf-8",
        )
        return str(target)
    except OSError:
        pass
    target = folder / f"ярлык_{stem}.txt"
    if target.exists():
        target, _ = unique_path(target)
    try:
        target.write_text(
            "Это указатель, а не копия.\n"
            f"Канонический файл: {source_abs}\n"
            "Создано Архивариусом МО. Правьте оригинал на месте.\n",
            "utf-8",
        )
        return str(target)
    except OSError:
        return ""


def undo_last(config: dict, progress=None) -> dict:
    output: Path = config["output_path"]
    root: Path = config["scan_root"]
    journals = sorted(output.glob("undo_*.json")) + sorted(output.glob("undo_*.jsonl"))
    if not journals:
        return {"файлов": 0, "ошибок": 0, "пачка": "", "откатывать": "нечего"}
    latest = max(journals, key=lambda p: p.stat().st_mtime)
    text = latest.read_text("utf-8")
    if latest.name.endswith(".jsonl"):
        steps = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        steps = json.loads(text)
    restored = failed = 0
    moved_back: list[tuple[str, str]] = []   # (куда вернулся файл, где лежал после переноса)
    log_rows = _load_log(output / "operations_log.csv")
    for position, step in enumerate(reversed(steps), 1):
        if progress:
            progress(position, len(steps), step.get("from", "") or step.get("создан", ""))
        if step.get("создан"):
            # Программа создала ярлык — при откате она его не удаляет, а уносит в служебную папку.
            created = Path(step["создан"])
            try:
                if not created.exists():
                    raise OSError("ярлык уже перемещён вручную")
                stash = output / "orphan_shortcuts"
                stash.mkdir(parents=True, exist_ok=True)
                parked, _ = unique_path(stash / created.name)
                shutil.move(str(created), str(parked))
                restored += 1
                log_rows.append({
                    "Дата": datetime.now().isoformat(timespec="seconds"),
                    "Действие": "откат ярлыка",
                    "Исходный путь": str(created),
                    "Новый путь": str(parked),
                    "Статус": "ok",
                    "Комментарий": f"откат пачки {latest.stem}; удалений нет",
                })
            except Exception as exc:
                failed += 1
                log_rows.append({
                    "Дата": datetime.now().isoformat(timespec="seconds"),
                    "Действие": "откат ярлыка",
                    "Исходный путь": str(created),
                    "Новый путь": "",
                    "Статус": f"ошибка: {type(exc).__name__}",
                    "Комментарий": str(exc)[:200],
                })
            continue
        src, dst = Path(step["from"]), Path(step["to"])
        try:
            if not src.exists():
                raise OSError("файл уже перемещён вручную")
            if dst.exists() and not (dst.parent == src.parent
                                     and dst.name.casefold() == src.name.casefold()):
                dst, _ = unique_path(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored += 1
            moved_back.append((
                (str(dst.relative_to(root)) if dst.is_relative_to(root) else str(dst)).replace("\\", "/"),
                (str(src.relative_to(root)) if src.is_relative_to(root) else str(src)).replace("\\", "/"),
            ))
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": "откат",
                "Исходный путь": str(src),
                "Новый путь": str(dst),
                "Статус": "ok",
                "Комментарий": f"откат пачки {latest.stem}",
            })
        except Exception as exc:
            failed += 1
            log_rows.append({
                "Дата": datetime.now().isoformat(timespec="seconds"),
                "Действие": "откат",
                "Исходный путь": str(src),
                "Новый путь": str(dst),
                "Статус": f"ошибка: {type(exc).__name__}",
                "Комментарий": str(exc)[:200],
            })
    _save_log(output / "operations_log.csv", log_rows)
    if moved_back:
        try:
            _restore_maps(output, moved_back)
        except Exception:
            pass   # файлы уже вернулись — карту починит следующее перечитывание
    if not failed:
        marker, _ = unique_path(latest.parent / (latest.name + ".done"))
        try:
            latest.rename(marker)
        except OSError:
            pass
    return {"пачка": latest.stem, "записей": len(steps), "возвращено": restored, "ошибок": failed}


def _restore_maps(output: Path, moved_back: list[tuple[str, str]]) -> None:
    """Файлы вернулись на старые места — верните и строки карты из истории в активную."""
    history = load_map(output / APPLIED_NAME) if (output / APPLIED_NAME).exists() else []
    active = load_map(output / MAP_NAME)
    hist_by = {(r.get("Текущий путь") or ""): r for r in history}
    act_by = {(r.get("Текущий путь") or ""): r for r in active}
    changed_a = changed_h = False
    for back_rel, applied_rel in moved_back:
        row = hist_by.get(applied_rel)
        if row is not None:
            history.remove(row)
            changed_h = True
            row["Текущий путь"] = back_rel
            row["Статус"] = "откат"
            row["Применить"] = "FALSE"
            row["Комментарий"] = ("откат пачкой; " + (row.get("Комментарий") or "")).strip("; ")[:400]
            active.append(row)
            act_by[back_rel] = row
            changed_a = True
            continue
        row = act_by.get(applied_rel) or act_by.get(back_rel)
        if row is not None:
            row["Статус"] = "откат"
            row["Применить"] = "FALSE"
            changed_a = True
    if changed_a:
        save_map(output / MAP_NAME, active)
    if changed_h:
        save_map(output / APPLIED_NAME, history)


def _load_log(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return [{key: (row.get(key) or "") for key in LOG_FIELDS} for row in csv.DictReader(stream, delimiter=";")]


def _save_log(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=LOG_FIELDS, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)

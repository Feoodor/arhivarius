"""Операции с ПАПКАМИ (не с файлами): пустые, двойники по имени, аккуратное слияние.

Философия та же, что у всего «Архивариуса»: программа только предлагает и
перемещает утверждённое; ничего не удаляет безвозвратно — пустые папки уходят
в корзину Windows (FOF_ALLOWUNDO), переносы пишутся в журнал folder_ops.csv и
откатываются пачкой. Удаления файлов здесь нет вообще.

Канон для двойников: одноимённые папки сливаются в ту, что НЕ внутри зоны
«Разобрать» и содержит больше файлов. Сливаем только когда контекст совпадает
(имена родителей похожи) или одна папка лежит ВНУТРИ другой — иначе «Музыка на
балу» и «Музыка на Пасхе» были бы объединены ошибочно. Осиротевшие оболочки
после слияния остаются в дереве и позже попадают в список «пустые».
"""
from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes
import json
import os
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

from .folders import is_garbled, norm, tokens

JOURNAL_NAME = "folder_ops.csv"
KEEP_NAME = "folder_keep.json"
PLAN_NAME = "folder_plan.json"
JUNK = {"desktop.ini", "thumbs.db", ".ds_store"}
GENERIC_NAMES = {
    "фото", "файлы", "документы", "материалы", "новое", "прочее", "разное", "черновики",
    "старое", "архив", "папка", "шаблоны", "2014", "2015", "2016", "2017", "2018", "2019",
    "2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027", "新建文件夹", "new folder",
}


# ---------------------------------------------------------------------- поиск
def iter_dirs(root: Path, prune_lower: set[str], max_depth: int = 10) -> list[str]:
    """Относительные пути всех папок под корнем (в prune-папки не заходим)."""
    out: list[str] = []
    for dirpath, dirnames, _files in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix().strip(".")
        depth = rel.count("/") + 1 if rel else 0
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".", "~$", "._")) and d.casefold() not in prune_lower]
        if depth > max_depth:
            dirnames[:] = []
        for d in dirnames:
            out.append(f"{rel + '/' if rel else ''}{d}")
    return sorted(set(out))


def is_dir_empty(path: Path) -> bool:
    try:
        return all(e.name.lower() in JUNK for e in os.scandir(path))
    except OSError:
        return False


def find_empty_folders(config: dict) -> tuple[list[str], list[str]]:
    """([кандидаты на корзину], [уже помеченные «не трогать»])."""
    root: Path = config["scan_root"]
    prune = {str(p).casefold() for p in config["excluded_dirs"]} | {config["output_path"].name.casefold()}
    keep = {k.casefold() for k in load_keep(config)}
    hidden = [str(p).replace("\\", "/").strip("/") for p in config.get("folders", {}).get("hide_prefixes", [])]
    trash = str(config.get("quarantine_dir", "")).strip("/").casefold()
    candidates: list[str] = []
    kept: list[str] = []
    for rel in iter_dirs(root, prune):
        low = rel.casefold()
        if trash and (low == trash or low.startswith(trash + "/")):
            continue
        if any(low == h.casefold() or low.startswith(h.casefold() + "/") for h in hidden):
            continue  # внутри зоны разбора — не наше дело
        if not is_dir_empty(root / PurePosixPath(rel)):
            continue
        (kept if low in keep else candidates).append(rel)
    return candidates, kept


def folder_name_map(index) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for path in index.by_path:
        if not path:
            continue
        name = path.rpartition("/")[2]
        key = norm(name)
        if not key or key in GENERIC_NAMES or is_garbled(name):
            continue
        groups.setdefault(key, []).append(path)
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def _hidden_prefixes(config: dict) -> list[str]:
    return [str(p).replace("\\", "/").strip("/") for p in config.get("folders", {}).get("hide_prefixes", [])]


def _in_hidden(path: str, hidden: list[str]) -> bool:
    low = path.casefold()
    return any(low == h.casefold() or low.startswith(h.casefold() + "/") for h in hidden)


def _parents_similar(a: str, b: str) -> bool:
    pa = norm(a.rpartition("/")[0].rpartition("/")[2])
    pb = norm(b.rpartition("/")[0].rpartition("/")[2])
    if not pa or not pb:
        return False
    if pa == pb:
        return True
    ta, tb = set(tokens(pa)), set(tokens(pb))
    return bool(ta & tb)


def find_merge_groups(config: dict, index) -> list[dict]:
    """Группы «одинаковое имя + подтверждённый контекст», годные к слиянию в канон.

    Сливаем только когда: (а) одна папка внутри другой; (б) у них ОДИН и тот же
    родитель (истинный дубль); (в) родители похожи И имя папки не общее — иначе
    «1 отделение» с разных балов склеилось бы в одну кучу."""
    hidden = _hidden_prefixes(config)
    groups: list[dict] = []
    for paths in folder_name_map(index).values():
        pool = [p for p in paths if not _in_hidden(p, hidden)]
        if len(pool) < 2:
            continue
        def score(pth: str) -> tuple[int, int, int]:
            node = index.by_path.get(pth)
            return (node.total if node else 0, -pth.count("/"), -len(pth))
        canonical = max(pool, key=score)
        name = canonical.rpartition("/")[2]
        distinctive = len(norm(name)) >= 8
        cparent = canonical.rpartition("/")[0]
        dups = []
        for pth in pool:
            if pth == canonical:
                continue
            if pth.startswith(canonical + "/") or canonical.startswith(pth + "/"):
                dups.append(pth)
            elif pth.rpartition("/")[0] == cparent:
                dups.append(pth)
            elif distinctive and _parents_similar(pth, canonical):
                dups.append(pth)
        if dups:
            groups.append({"name": name, "canonical": canonical, "duplicates": sorted(dups)})
    return groups


# ------------------------------------------------------------------ операции
def plan_merge(config: dict, group: dict) -> list[dict]:
    """Что физически перенесём при слиянии: содержимое каждого дубля в канон."""
    root: Path = config["scan_root"]
    known = index_paths(config)
    moves: list[dict] = []
    canonical = group["canonical"]
    for dup in group["duplicates"]:
        src = root / PurePosixPath(dup)
        if not src.is_dir():
            continue
        try:
            entries = sorted(os.scandir(src), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.lower() in JUNK:
                continue
            if canonical.startswith(dup + "/") and entry.name == canonical[len(dup) + 1:].split("/")[0]:
                continue  # это сам канон внутри дубля — не трогаем
            куда = f"{canonical}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                moves.append({"откуда": f"{dup}/{entry.name}", "куда": куда,
                              "режим": "merge-dir" if куда in known else "move"})
            else:
                moves.append({"откуда": f"{dup}/{entry.name}", "куда": куда, "режим": "move"})
    return moves


_IDX_CACHE: dict[str, set[str]] = {}


def index_paths(config: dict) -> set[str]:
    key = str(config["output_path"])
    if key not in _IDX_CACHE:
        from . import pipeline
        try:
            _snap, idx = pipeline._snapshot_and_index(config)
            _IDX_CACHE[key] = set(idx.by_path)
        except Exception:
            _IDX_CACHE[key] = set()
    return _IDX_CACHE[key]


def invalidate_index_cache() -> None:
    _IDX_CACHE.clear()


def unique_name(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, ext = dst.stem, dst.suffix
    for n in range(2, 500):
        cand = dst.with_name(f"{stem}_v{n}{ext}")
        if not cand.exists():
            return cand
    return dst.with_name(f"{stem}_v{int(time.time()) % 100000}{ext}")


def apply_move(config: dict, move: dict, batch: str) -> dict:
    """Один перенос (или рекурсивное соединение папок) с записью в журнал."""
    root: Path = config["scan_root"]
    src = root / PurePosixPath(move["откуда"])
    dst = root / PurePosixPath(move["куда"])
    if not src.exists():
        return {"ok": False, "msg": "источник пропал"}
    if move.get("режим") == "merge-dir":
        results: list[dict] = []
        dst.mkdir(parents=True, exist_ok=True)
        for entry in sorted(os.scandir(src), key=lambda e: e.name):
            sub = {"откуда": f"{move['откуда']}/{entry.name}",
                   "куда": f"{move['куда']}/{entry.name}",
                   "режим": "merge-dir" if entry.is_dir(follow_symlinks=False) else "move"}
            results.append(apply_move(config, sub, batch))
        return {"ok": all(r.get("ok") for r in results), "msg": f"merge x{len(results)}"}
    if src.is_dir() and dst.is_dir():
        return apply_move(config, {**move, "режим": "merge-dir"}, batch)
    dst.parent.mkdir(parents=True, exist_ok=True)
    final = unique_name(dst)
    try:
        import shutil
        shutil.move(str(src), str(final))
        log_op(config, "перенести", move["откуда"], final.relative_to(root).as_posix(), batch, "ok")
        return {"ok": True, "msg": final.name}
    except OSError as exc:
        log_op(config, "перенести", move["откуда"], move["куда"], batch, f"ОШИБКА: {exc}")
        return {"ok": False, "msg": str(exc)}


def move_folder(config: dict, src_rel: str, dst_parent_rel: str, batch: str) -> dict:
    """Перенос целой папки в другой раздел (для плана «Разобрать → корень»).
    Если так же названная папка там уже есть — сливаем содержимое."""
    root: Path = config["scan_root"]
    src = root / PurePosixPath(src_rel)
    name = src_rel.rpartition("/")[2] or src_rel
    target = f"{dst_parent_rel.strip('/')}/{name}".strip("/")
    if not src.is_dir():
        return {"ok": False, "msg": "нет такой папки"}
    mode = "merge-dir" if (root / PurePosixPath(target)).is_dir() else "move"
    return apply_move(config, {"откуда": src_rel, "куда": target, "режим": mode}, batch)


def undo_batch(config: dict, batch: str) -> dict:
    """Обратный прогон пачки переносов; созданные пустые папки остаются (их чистят отдельно)."""
    root: Path = config["scan_root"]
    moved = [r for r in read_journal(config)
             if r["Пачка"] == batch and r["Действие"] == "перенести" and r["Статус"] == "ok"]
    restored = errors = 0
    for row in reversed(moved):
        src = root / PurePosixPath(row["Куда"])
        dst = root / PurePosixPath(row["Откуда"])
        if not src.exists():
            errors += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            import shutil
            shutil.move(str(src), str(unique_name(dst)) if dst.exists() else str(dst))
            log_op(config, "откат", row["Куда"], row["Откуда"], f"откат-{batch}", "ok")
            restored += 1
        except OSError:
            errors += 1
    return {"вернуто": restored, "ошибок": errors}


# ------------------------------------------------------------------ журнал
def journal_path(config: dict) -> Path:
    return config["output_path"] / JOURNAL_NAME


def log_op(config: dict, action: str, src: str, dst: str, batch: str, status: str) -> None:
    path = journal_path(config)
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        if new:
            writer.writerow(["Дата", "Действие", "Откуда", "Куда", "Пачка", "Статус"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), action, src, dst, batch, status])


def read_journal(config: dict) -> list[dict]:
    path = journal_path(config)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, delimiter=";"))


# --------------------------------------------------------------- «не трогать»
def load_keep(config: dict) -> list[str]:
    path = config["output_path"] / KEEP_NAME
    try:
        return [str(x) for x in json.loads(path.read_text("utf-8"))]
    except Exception:
        return []


def mark_keep(config: dict, rel: str, keep: bool) -> list[str]:
    items = {k for k in load_keep(config)}
    if keep:
        items.add(rel)
    else:
        items.discard(rel)
    save_keep(config, sorted(items))
    return sorted(items)


def save_keep(config: dict, items: list[str]) -> None:
    (config["output_path"] / KEEP_NAME).write_text(
        json.dumps(sorted(set(items)), ensure_ascii=False, indent=2), "utf-8")


# ----------------------------------------------------------------- корзина
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.wintypes.HWND), ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]


def recycle_folder(config: dict, rel: str) -> tuple[bool, str]:
    """Пустая папка — в корзину Windows (восстановимо). Непустую не трогаем."""
    abs_path = config["scan_root"] / PurePosixPath(rel)
    if not abs_path.is_dir():
        return False, "не папка"
    if not is_dir_empty(abs_path):
        return False, "папка не пуста — пропускаем"
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = os.fsdecode(abs_path) + "\0\0"
    op.fFlags = 0x0040 | 0x0004 | 0x0010  # ALLOWUNDO | NOCONFIRMATION | SILENT
    try:
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        ok = res == 0 and not op.fAnyOperationsAborted
        log_op(config, "в-корзину", rel, "", datetime.now().strftime("%Y%m%d-%H%M%S"),
               "ok" if ok else f"код {res}")
        return ok, "" if ok else f"код {res}"
    except OSError as exc:
        return False, str(exc)


# --------------------------------------------------------------- план (модель)
def load_plan(config: dict) -> list[dict]:
    path = config["output_path"] / PLAN_NAME
    try:
        return list(json.loads(path.read_text("utf-8")))
    except Exception:
        return []


def save_plan(config: dict, rows: list[dict]) -> None:
    (config["output_path"] / PLAN_NAME).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")


# ------------------------------------------------- подсказки таксономии
def taxonomy_hints(config, min_count: int = 3) -> list[dict]:
    """Папки, которые модель просит создать снова и снова (≥min_count раз).

    Если несколько разных файлов просят одну и ту же новую папку под одним
    родителем — это не выдумка, а недостающий раздел структуры. Предлагаем
    создать её один раз (mkdir, без переносов)."""
    import json as _json
    import re as _re
    src = config["output_path"] / "suggestions.json"
    try:
        rows = _json.loads(src.read_text("utf-8"))
    except Exception:
        return []
    pat = _re.compile(r"создаётся одна новая папка «(.+?)»")
    tally: dict[tuple[str, str], int] = {}
    for row in rows:
        m = pat.search(str(row.get("_исправления") or ""))
        if not m:
            continue
        target = str(row.get("Целевой путь") or "")
        parent = target.rpartition("/")[0]
        if not parent:
            continue
        key = (parent, m.group(1))
        tally[key] = tally.get(key, 0) + 1
    return [{"родитель": par, "имя": name, "сколько": n}
            for (par, name), n in sorted(tally.items()) if n >= min_count]


# ------------------------------------------------- статистика и прогноз
TEXT_EXT = {".docx", ".doc", ".pdf", ".rtf", ".odt", ".txt", ".md", ".xlsx", ".xls",
            ".pptx", ".csv", ".tsv", ".html", ".htm", ".xml", ".msg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}


def folder_stats(snapshot: dict, folder_rel: str) -> dict:
    """Сколько в ветке файлов: текст и картинки отдельно (для прогноза времени)."""
    pre = folder_rel.rstrip("/") + "/"
    text = images = other = 0
    for item in snapshot.get("files", []):
        p = str(item.get("path", "")).replace("\\", "/")
        if not p.startswith(pre):
            continue
        ext = "." + p.rpartition(".")[2].lower()
        if ext in TEXT_EXT:
            text += 1
        elif ext in IMAGE_EXT:
            images += 1
        else:
            other += 1
    return {"текст": text, "картинки": images, "прочее": other,
            "всего": text + images + other}


PROBE_NAME = "speed_probe.json"


def load_probe(config) -> dict:
    try:
        return json.loads((config["output_path"] / PROBE_NAME).read_text("utf-8"))
    except Exception:
        return {}


def measure_speed(config, log=None) -> dict:
    """По одному реальному файлу текста и картинки — секунды на разбор каждого.

    Это калибровка прогноза: партия из N текстовых и M картинок займёт примерно
    N·t_текст + M·t_картинка. Два запроса к модели ~1–2 минуты (с прогревом)."""
    import time as _time
    from . import pipeline
    log = log or (lambda m: None)
    snapshot, index = pipeline._snapshot_and_index(config)
    an = pipeline.Analyzer(config, snapshot, index, log=log)
    an.preflight()
    hidden = [str(h).strip("/").casefold() for h in config.get("folders", {}).get("hide_prefixes", [])]
    text_file = image_file = None
    for item in snapshot.get("files", []):
        p = str(item.get("path", "")).replace("\\", "/")
        ext = "." + p.rpartition(".")[2].lower()
        if any(p.casefold().startswith(h + "/") for h in hidden):
            continue
        if text_file is None and ext in {".docx", ".pdf", ".txt", ".md"} and (config["scan_root"] / p).exists():
            text_file = item
        if image_file is None and ext in {".jpg", ".jpeg", ".png"} and (config["scan_root"] / p).exists():
            image_file = item
        if text_file and image_file:
            break
    out: dict = {"дата": datetime.now().isoformat(timespec="seconds")}
    if text_file:
        t0 = _time.monotonic()
        an.process_one(text_file)
        out["t_текст"] = round(_time.monotonic() - t0, 1)
        log(f"  замер текст: {out['t_текст']}с ({str(text_file['path']).rpartition('/')[2][:40]})")
    if image_file:
        t0 = _time.monotonic()
        an.process_one(image_file)
        out["t_картинка"] = round(_time.monotonic() - t0, 1)
        log(f"  замер картинка: {out['t_картинка']}с ({str(image_file['path']).rpartition('/')[2][:40]})")
    if len(out) > 1:
        try:
            (config["output_path"] / PROBE_NAME).write_text(
                json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            pass
    return out


def estimate(stats: dict, probe: dict) -> str:
    """«≈ 12 мин» по статистике папки и калибровке; '' если калибровки нет."""
    if not probe:
        return ""
    tt = float(probe.get("t_текст") or 0)
    ti = float(probe.get("t_картинка") or 0)
    secs = stats.get("текст", 0) * tt + stats.get("картинки", 0) * ti
    if secs <= 0:
        return ""
    mins = secs / 60
    if mins < 1:
        return f"≈ {int(secs)} с"
    if mins < 60:
        return f"≈ {round(mins)} мин"
    return f"≈ {mins/60:.1f} ч"

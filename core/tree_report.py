"""Структура папок из снимка: что где лежит и сколько. Считает код, модель не нужна."""
from __future__ import annotations

from collections import Counter, defaultdict


def build_lines(snapshot: dict, hidden: list[str] | None = None, max_ext: int = 4) -> tuple[list[str], dict]:
    """Вернуть (строки отчёта, сводка). Одна строка на папку, отступ — по глубине."""
    hidden_set = {str(h).replace("\\", "/").strip("/").casefold() for h in (hidden or [])}
    direct: dict[str, int] = defaultdict(int)
    direct_size: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    total_size: dict[str, int] = defaultdict(int)
    exts: dict[str, Counter] = defaultdict(Counter)

    for item in snapshot.get("files", []):
        rel = str(item.get("path", "")).replace("\\", "/")
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        size = int(item.get("size") or 0)
        direct[parent] += 1
        direct_size[parent] += size
        exts[parent][str(item.get("extension") or "(без расширения)")] += 1
        # суммы по всем предкам, чтобы у папки было «всего внутри»
        cur = parent
        while True:
            total[cur] += 1
            total_size[cur] += size
            if not cur:
                break
            cur = cur.rsplit("/", 1)[0] if "/" in cur else ""

    folders = sorted(set(snapshot.get("folders", [])) | {""})
    lines = ["# Структура папок (построено из снимка, без модели)",
             "# формат: папка — файлов прямо / всего внутри, размер всего, частые расширения", ""]
    for folder in folders:
        depth = folder.count("/") + 1 if folder else 0
        name = folder.rsplit("/", 1)[-1] if folder else "(корень скана)"
        mark = ""
        low = folder.casefold()
        if any(low == h or low.startswith(h + "/") for h in hidden_set):
            mark = "  [скрыта в разборе]"
        top = exts[folder].most_common(max_ext)
        ext_text = ", ".join(f"{e} {n}" for e, n in top) or "нет файлов"
        lines.append(
            f"{'  ' * depth}{name} — {direct[folder]} / {total[folder]} файл(ов), "
            f"{total_size[folder] / 1e6:.1f} МБ; {ext_text}{mark}"
        )
    summary = {
        "папок": len(folders) - 1,
        "файлов": len(snapshot.get("files", [])),
        "размер_МБ": round(sum(int(i.get("size") or 0) for i in snapshot.get("files", [])) / 1e6, 1),
        "пустых_папок": sum(1 for f in folders if f and total[f] == 0),
    }
    return lines, summary

"""Похожие фотографии (перцептуальный хеш), а не только байт-в-байт.

Точный sha256 ловит идеальные копии. Пересжатые, переименованные, слегка
повернутые дубли Telegram/WhatsApp он не видит — а их в архиве большинство.
Здесь — dhash: картинка сжимается до 9×8 в сером, сравниваются соседние
пиксели → 64 бита «отпечатка». Совпадение ≤5 бит из 64 — почти наверняка тот
же кадр. Ничего не перемещаем и не удаляем: пишем similar_photos.csv, решает
человек (как и обычные дубликаты).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}


def dhash(path: Path, size: int = 8) -> int | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("L").resize((size + 1, size), Image.LANCZOS)
            px = list(img.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def find_similar(config, prefixes: list[str] | None = None, max_files: int = 4000,
                 threshold: int = 5) -> list[dict]:
    """Группы похожих картинок: [{файл, дубликат, расстояние}, ...]"""
    root: Path = config["scan_root"]
    prune = {str(p).casefold() for p in config["excluded_dirs"]} | {config["output_path"].name.casefold()}
    items: list[tuple[int, str]] = []
    scanned = 0
    for dirpath, dirnames, filenames in __import__("os").walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix().strip(".")
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".", "~$", "._")) and d.casefold() not in prune]
        if prefixes and not any(rel_dir.startswith(p.strip("/")) for p in prefixes):
            continue
        for f in filenames:
            if Path(f).suffix.lower() not in IMAGE_EXT:
                continue
            scanned += 1
            if scanned > max_files:
                break
            sig = dhash(Path(dirpath) / f)
            if sig is not None:
                rel = (rel_dir + "/" if rel_dir else "") + f
                items.append((sig, rel))
        if scanned > max_files:
            break
    # быстрый грубый отбор: корзинам по точному dhash, затем соседним расстоянием
    exact: dict[int, list[str]] = defaultdict(list)
    for sig, rel in items:
        exact[sig].append(rel)
    rows: list[dict] = []
    seen: set[frozenset] = set()
    for sig, paths in exact.items():
        if len(paths) > 1:
            base = sorted(paths)[0]
            for p in sorted(paths)[1:]:
                rows.append({"файл": base, "дубликат": p, "расстояние": 0})
    sigs = list(exact.items())
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            d = hamming(sigs[i][0], sigs[j][0])
            if 0 < d <= threshold:
                pair = frozenset((sigs[i][1][0], sigs[j][1][0]))
                if pair in seen:
                    continue
                seen.add(pair)
                a, b = sorted((sigs[i][1][0], sigs[j][1][0]))
                rows.append({"файл": a, "дубликат": b, "расстояние": d})
    return sorted(rows, key=lambda r: (r["расстояние"], r["файл"]))


def save_report(config, rows: list[dict]) -> Path:
    target = config["output_path"] / "similar_photos.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["файл", "дубликат", "расстояние"],
                                delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return target

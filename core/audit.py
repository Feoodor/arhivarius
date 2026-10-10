"""Аудит «странных» строк карты (замена «Вынести уроки» по просьбе Фёдора).

Кнопка смотрит на ВСЮ карту и детерминированными эвристиками (без модели) находит
строки с подозрительным паттерном — то, что человек интуитивно назвал бы «странно»:

  • завалялся в развале и не разобран (остался в «00_Разобрать/Разное/Загрузки/…»,
    имя не тронуто или цель = текущая папка);
  • мусорное имя (IMG_2041, Scan0003, WhatsApp…, «копия», «(1)/(2)», «final», uuid,
    только цифры/дата, двойные пробелы);
  • битая цель — папки нет на карте и это не новый уровень внутри существующей;
  • уверенно, но даты расходятся — в имени один год, в папке проведения другой,
    обе уверенности высокие (типичная «тихая» ошибка);
  • столкновение — два файла претендуют на одно имя в одной папке;
  • утверждено, но неполно — стоит «Применить=TRUE», а имя или папка пусты.

Функция только ПОМЕЧАЕТ строки и объясняет почему; она ничего не переносит и не
переименовывает. Дальше человек запускает мягкую перепроверку этих строк моделью
(«🩺 Проверить странное») — и снова утверждает результат сам.
"""
from __future__ import annotations

import re
from pathlib import Path

# Папки-«развалы»: здесь файл ещё не разобран, если имя/место не тронуты.
INBOX_HINTS = {
    "00_разобрать", "разобрать", "разное", "прочее", "загрузки", "downloads",
    "telegram desktop", "рабочий стол", "desktop", "входящие", "вх", "new", "новые",
    "не разобрано",
}

# Мусорные маркеры в имени.
_JUNK_RE = re.compile(
    r"(img[_-]?\d|scan[_-]?\d|whatsapp|wechat|telegram| фото |video|dsc[_-]?\d|"
    r"gopr|camera|snapseed|\bfinal\b|\bnew\b|\bok\b|copy|копия|черновик|без\s*имени|"
    r"^\s*\d+\s*$|\(\d+\)|\s{2,})",
    re.IGNORECASE,
)
# Файл с бессмысленным именем-«шифром»: 8+ hex или uuid.
_HEX_RE = re.compile(r"^[0-9a-f]{8,}(-[0-9a-f]{4}){3,}", re.IGNORECASE)
# Четырёхзначный год внутри строки.
_YEAR_RE = re.compile(r"(19|20)\d{2}")
# Имя «как надо» по формату prompt.md: начинается с даты ГГГГ… и блоки через «_».
_FORMATTED_RE = re.compile(r"^((19|20)\d{2}(-\d{2}(-\d{2})?)?_|TPL_|Вх-|Исх-)", re.IGNORECASE)


def _fold(s: str) -> str:
    return (s or "").casefold().strip()


def _num_conf(row: dict, key: str) -> float | None:
    val = row.get(key)
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _in_inbox(rel: str) -> bool:
    segs = {_fold(p) for p in rel.split("/")[:-1]}
    return bool(segs & INBOX_HINTS)


def find_anomalies(rows: list[dict], index=None) -> list[dict]:
    """Возвращает [{"rel","name","reasons":[..]}] для строк с подозрительным паттерном."""
    out: list[dict] = []
    by_path = set(getattr(index, "by_path", {}) or ()) if index is not None else None

    # столкновения считаем заранее
    collisions: dict[tuple[str, str], int] = {}
    for row in rows:
        tgt = _fold(str(row.get("Целевой путь", "") or ""))
        nm = _fold(str(row.get("Новое имя", "") or ""))
        if tgt and nm:
            collisions[(tgt, nm)] = collisions.get((tgt, nm), 0) + 1

    for row in rows:
        rel = str(row.get("Текущий путь", "") or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        status = _fold(str(row.get("Статус", "") or ""))
        apply_mark = _fold(str(row.get("Применить", "") or ""))
        if status == "перенесено":  # закрытая строка — не трогаем
            continue

        cur_name = str(row.get("Текущее имя", "") or Path(rel).name)
        cur_dir = rel.rpartition("/")[0]
        new_name = str(row.get("Новое имя", "") or "").strip()
        target = str(row.get("Целевой путь", "") or "").strip().strip("/")
        name_c = _num_conf(row, "Уверенность имени")
        move_c = _num_conf(row, "Уверенность переноса")
        reasons: list[str] = []

        effective_name = new_name or cur_name

        # 1. Мусорное имя
        if _JUNK_RE.search(effective_name) or _HEX_RE.match(Path(effective_name).stem):
            reasons.append("мусорное имя")

        # 2. Завалялся в развале без разбора
        if _in_inbox(rel):
            unchanged_name = (not new_name) or (_fold(new_name) == _fold(cur_name))
            no_move = (not target) or (_fold(target) == _fold(cur_dir))
            if unchanged_name and no_move:
                reasons.append("в развале: имя и папка не тронуты")
            elif no_move:
                reasons.append("в развале: предложено не переносить")

        # 3. Битая цель (папки нет и это не новый уровень внутри существующей)
        if target and by_path is not None and target not in by_path:
            parent = target.rpartition("/")[0]
            if not (parent in by_path or not parent):
                reasons.append("нет такой папки на карте")

        # 4. Уверенно, но годы расходятся
        if target and effective_name and name_c and move_c and name_c >= 0.8 and move_c >= 0.8:
            nm_years = {m[0] + m[1] for m in _YEAR_RE.finditer(effective_name)}
            tg_years = {m[0] + m[1] for m in _YEAR_RE.finditer(target)}
            if nm_years and tg_years and not (nm_years & tg_years):
                reasons.append("имя и год папки расходятся")

        # 5. Столкновение с другой строкой
        if target and new_name and collisions.get((_fold(target), _fold(new_name)), 0) > 1:
            reasons.append("два файла на одно имя")

        # 6. Утверждено, но неполно
        if apply_mark == "true" and (not new_name or not target):
            reasons.append("отмечено, но имя или папка пусты")

        if reasons:
            out.append({"rel": rel, "name": cur_name, "reasons": reasons})
    return out


def summarize(anomalies: list[dict], cap: int = 8) -> str:
    lines = [f"Странных строк: {len(anomalies)}"]
    tally: dict[str, int] = {}
    for a in anomalies:
        for r in a["reasons"]:
            tally[r] = tally.get(r, 0) + 1
    for reason, cnt in sorted(tally.items(), key=lambda kv: -kv[1]):
        lines.append(f"  · {reason}: {cnt}")
    lines.append("примеры:")
    for a in anomalies[:cap]:
        lines.append(f"  — {Path(a['rel']).name}  [{', '.join(a['reasons'])}]")
    return "\n".join(lines)

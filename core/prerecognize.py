"""Правила раньше модели: детерминированные решения без обращения к LLM.

Идея (как «More Consistent» у аналогов): если ответ однозначно следует из имён и
фактов — не надо гадать моделью. Это быстрее и стабильнее. Что считается
однозначным:

  R1  файл уже назван по регламенту И лежит не в зоне разбора → «без изменений»;
  R2  имя/путь файла называет существующую папку события (не «Разобрать») →
      целевая папка = та самая папка, имя не трогаем;
  R3  тот же sha256, что у уже разобранного файла карты → решение наследуется;
  R4  служебные ярлыки Google/линки (0 байт, не читаются) → сразу «проверка»
      без запроса (модель всё равно не имеет данных);
  R6  скриншот (по имени или размеру без EXIF) → в «Дизайн/Скриншоты», если она есть.

Каждое попадание помечается в записи «правило» — видно, что решала не модель.
Порог доверия к правилам консервативный: лучше пропустить и отдать модели.
"""
from __future__ import annotations

import re
from pathlib import Path

CANON_HEAD = re.compile(r"^(?:19|20)\d{2}(?:-\d{2}){0,2}_[А-ЯЁA-Z][\w-]+_.+\.[A-Za-zА-Яа-яЁё0-9]{2,5}$")
SCREEN_NAME = re.compile(r"(снимок\s*экрана|screen_?shot|screenshot|screen\d|экран\d)", re.I)
SHORTCUT_EXT = {".lnk", ".url", ".gdoc", ".gsheet", ".gslides", ".gform", ".gdraw",
                ".gsite", ".gftx", ".gslides", ".desktop", ".website"}
EVENT_MIN_TOKENS = 2      # «2026» или «Фото» само по себе — не сигнал события


def _intake_prefixes(config: dict) -> list[str]:
    return [str(p).replace("\\", "/").strip("/").casefold()
            for p in config.get("folders", {}).get("hide_prefixes", [])]


def _in_intake(rel: str, config: dict) -> bool:
    low = rel.casefold()
    return any(low == h or low.startswith(h + "/") for h in _intake_prefixes(config))


def deterministic(item: dict, index, config: dict) -> dict | None:
    """Ответ в формате схемы модели, если правило сработало; иначе None."""
    rel = str(item.get("path", "")).replace("\\", "/")
    name = rel.rpartition("/")[2]
    parent = rel.rpartition("/")[0]
    intake = _in_intake(rel, config)
    base = {"doc_type": "", "direction": "", "reg_number": "", "doc_date": "",
            "event_name": "", "event_start": "", "event_end": "", "shortcut_to": "",
            "name_confidence": 0.95, "move_confidence": 0.95, "confidence": 0.95,
            "review": False, "discard": False, "discard_reason": ""}

    # R4 — ярлыки и ссылки: модель тут бессильна, честно на проверку
    if Path(name).suffix.lower() in SHORTCUT_EXT:
        return {**base, "new_name": name, "target_path": parent,
                "name_confidence": 0.5, "move_confidence": 0.5, "review": True,
                "reason": "правило: служебный ярлык/ссылка — содержимое не читается, решает человек"}

    # R1 — имя уже по регламенту, файл не в зоне разбора: ничего не делаем
    if not intake and CANON_HEAD.match(name) and parent and parent in index.by_path:
        return {**base, "new_name": name, "target_path": parent,
                "reason": "правило: имя по регламенту и лежит в постоянной папке — менять нечего"}

    # R6 — скриншоты
    if SCREEN_NAME.search(name):
        shot = next((p for p in index.by_path if p.casefold().endswith("дизайн/скриншоты")), "")
        if shot:
            return {**base, "new_name": name, "target_path": shot, "doc_type": "Изображение",
                    "reason": f"правило: скриншот — существующая папка «{shot}»"}

    # R2 — имя файла называет существующую папку события (не зону разбора)
    hidden = _intake_prefixes(config)
    low_name = (" " + re.sub(r"[_\-.]+", " ", name).casefold().replace("ё", "е") + " ")
    hits: list[tuple[int, str]] = []
    for path, node in index.by_path.items():
        if not path or node.depth < 2 or node.total == 0:
            continue
        low_path = path.casefold().replace("ё", "е")
        if any(low_path == h or low_path.startswith(h + "/") for h in hidden):
            continue
        folder_tail = " " + re.sub(r"[_\-.]+", " ", low_path.rpartition("/")[2]) + " "
        words = [w for w in folder_tail.split() if len(w) >= 4]
        if len(words) < EVENT_MIN_TOKENS:
            continue
        if all(w in low_name for w in words):
            hits.append((len(words), path))
    if hits:
        # нюансы «от кого/кому» и номера Вх/Исх — дело модели: письмо при упоминании
        # события всё равно письмо
        if re.search(r"(вх|исх)[-_ ]?\d", low_name) or " от " in low_name or " кому" in low_name:
            return None
        hits.sort(key=lambda t: (-t[0], t[1]))
        target = hits[0][1]
        # год в имени файла: если внутри папки есть подпапка с этим годом — целимся в неё
        m_year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", name)
        if m_year:
            year = m_year.group(0)
            kid = target + "/" + year
            kid_fold = kid.casefold().replace("ё", "е")
            match = next((p for p in index.by_path
                          if p.casefold().replace("ё", "е") == kid_fold), "")
            if not match:  # может, год живёт в названии подпапки («2026-07_Летний_слёт»)
                pref = kid_fold + "_"
                cands = [pp for pp in index.by_path if pp.casefold().replace("ё", "е").startswith(pref)]
                match = cands[0] if len(cands) == 1 else ""
            if match:
                target = match
        if target != parent:
            return {**base, "new_name": name, "target_path": target,
                    "reason": "правило: имя файла называет существующую папку «"
                              + target.rpartition("/")[2] + "»"}

    # R3 — тот же хеш уже разобран в этой партии (свой слой у Analyzer._hash_seen);
    # здесь только подсказка: кросс-запусковый поиск по suggestions делает pipeline
    return None

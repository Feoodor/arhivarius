"""Память утверждённых решений: few-shot из того, что человек уже подтвердил.

Аналоги (ai-file-sorter и др.) называют это «recent similar results»: модель
значительно точнее, когда рядом с задачей лежат 2–3 свежих ПРИМЕРА с правильным
ответом. Мы храним такие примеры из уже применённой карты (перенесённые и
подтверждённые «без изменений» строки) и подмешиваем в запрос самые похожие по
словам. Ничего не переучиваем — просто показываем прецеденты.

Файл: output/fewshot.json (кэш, пересобирается из applied-карты).
"""
from __future__ import annotations

import csv
import json
import re

from .folders import keywords_from_text, stem
from .folders import tokens as _split

STORE_NAME = "fewshot.json"
MAX_CASES = 400


def _tokens_of(text: str) -> set[str]:
    # те же нормализация (ё→е) и стем, что у keywords_from_text — иначе прецеденты
    # не пересекаются с запросом («слёт» vs «слет»)
    return {stem(w) for w in _split(text or "") if len(w) >= 4}


def refresh(config) -> int:
    """Собрать примеры из migration_map_applied.csv (свежие сверху)."""
    applied = config["output_path"] / "migration_map_applied.csv"
    cases: list[dict] = []
    if applied.exists():
        try:
            with applied.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter=";"))
        except Exception:
            rows = []
        for row in reversed(rows):
            old = (row.get("Текущий путь") or "").replace("\\", "/")
            new = (row.get("Новое имя") or "").strip()
            target = (row.get("Целевой путь") or "").strip()
            reason = (row.get("Причина") or "").strip()[:180]
            if not old or not new:
                continue
            cases.append({"откуда": old, "имя": new, "куда": target, "почему": reason,
                          "токены": sorted(_tokens_of(f"{old} {new} {target}"))})
            if len(cases) >= MAX_CASES:
                break
    try:
        import os

        target = config["output_path"] / STORE_NAME
        tmp = target.with_name(STORE_NAME + ".tmp")
        tmp.write_text(json.dumps(cases, ensure_ascii=False), "utf-8")
        os.replace(tmp, target)
    except OSError:
        pass
    return len(cases)


def hints(config, rel: str, text: str, k: int = 3, min_overlap: int = 3) -> str:
    """Блок «утверждённые прецеденты» для промпта ('' если ничего похожего)."""
    path = config["output_path"] / STORE_NAME
    try:
        cases = json.loads(path.read_text("utf-8"))
    except Exception:
        return ""
    if not cases:
        return ""
    want = {stem(w) for w in keywords_from_text(f"{rel} {text[:900]}", limit=24)}
    scored = []
    for case in cases:
        overlap = len(want & set(case.get("токены", [])))
        if overlap >= min_overlap:
            scored.append((overlap, case))
    if not scored:
        return ""
    scored.sort(key=lambda t: -t[0])
    lines = ["УТВЕРЖДЁННЫЕ РЕШЕНИЯ ПОХОЖИХ ФАЙЛОВ (прецеденты человека — ориентир, не догма):"]
    for _, case in scored[:k]:
        lines.append(f"  • {case['откуда'].rpartition('/')[2]} → {case['куда'] or '(оставили)'}/{case['имя']}"
                     + (f" — {case['почему'][:90]}" if case.get("почему") else ""))
    return "\n".join(lines)

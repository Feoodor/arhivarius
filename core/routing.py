"""Детерминированный распознаватель «материал другого проекта».

Отдел лежит в одном дереве с материалами смежных проектов (движение Theosis,
Theosis Studio, кафе «Agapa», приход Юровичская/Кравцовка, Благорез, семья).
Слабая модель часто роняет такой материал в общие разделы отдела. Здесь —
дешёвый и ГЛАВНОЕ ВЫСОКОТОЧНЫЙ сигнал по пути/имени файла: срабатываем только
на явных брендовых именах (routing.external + константы ниже), поэтому общие
слова («приход», «слёт», «молодёжь», «Сретение») НЕ вызывают ложных срабатываний.

Договорённость с Фёдором (2026-10-09): материалы чужих дисков программа кладёт
в их дом внутри структуры отдела — обычно это ветки «Направления…» (Theosis
Studio, Движение Theosis, Кафе «Agapa») или «Личное - другие проекты/…»
(Благорез, приходы). Своего дома нет (например «Семья», пустой диск Agapa) —
предлагаем ОДНУ новую подпапку в «Личное - другие проекты», строка идёт на
проверку: оттуда Фёдор сам переложит на настоящий диск.

Мы НЕ перемещаем файл и НЕ переопределяем решение модели — только даём явную
подсказку; итог и перенос остаются за человеком.
"""
from __future__ import annotations

import re

from .folders import is_garbled, stem, tokens

MAX_DEPTH = 4      # проект ищем по его корневой/проектной папке, не по глубокой ветке
_STOP_BRAND = {"приход", "храм", "кафе", "студия", "движение", "проект", "проекты",
               "отдел", "молодежный", "молодёжный", "gomel", "гомель"}
# Короткие бренды, которых режет фильтр длины (agapa=5): заносим напрямую.
_FORCED_BRANDS = {"agapa": "Agapa", "агап": "Agapa", "theosis": "Theosis",
                  "благорез": "Благорез"}
# «Семья» — слишком обиходное слово: верим только точной форме (не «семейный»).
_EXACT_FORMS = {"семья": "Семья", "семьи": "Семья", "семей": "Семья"}


def _brand_info(config: dict) -> dict[str, str]:
    """stem бренда → отображаемое имя проекта (для подсказки и фолбэк-папки)."""
    names = list((config.get("routing", {}) or {}).get("external", {}) or {})
    names += ["Theosis", "Agapa", "Агапа", "Благорез"]
    out: dict[str, str] = {}
    for n in names:
        display = re.sub(r"[_\s]+", " ", str(n)).strip() or "?"
        # «Приход_Юровичская» → «Юровичская»; «Благорез» → «Благорез»
        for part in re.split(r"[_\s]+", str(n)):
            for tok in tokens(part):
                if (len(tok) >= 6 or tok in _FORCED_BRANDS) and tok not in _STOP_BRAND:
                    out.setdefault(stem(tok), display)
    for tok, display in _FORCED_BRANDS.items():
        out.setdefault(stem(tok), display)
    return out


def _src_brand_hit(rel: str, stems: set[str]) -> set[str]:
    """Ключи найденных брендов: stem либо «!точное-слово» для обиходных маркеров."""
    hits: set[str] = set()
    for p in (rel or "").replace("\\", "/").split("/"):
        if not p or is_garbled(p):
            continue
        for tok in tokens(p):
            if tok in _EXACT_FORMS:
                hits.add("!" + tok)
            else:
                s = stem(tok)
                if s in stems:
                    hits.add(s)
    return hits


def project_homes(rel: str, index, roots: list[str], config: dict) -> dict:
    """{brand, homes:[…существующие папки-дома…], fallback:'Личное…/<Бренд>'} или {}."""
    brands = _brand_info(config)
    hit = _src_brand_hit(rel, set(brands))
    if not hit:
        return {}
    display = ""
    name_keys: set[str] = set()
    for h in sorted(hit, key=len, reverse=True):
        if h.startswith("!"):
            word = h[1:]
            name_keys |= {word, stem(word)}
            display = display or _EXACT_FORMS.get(word, "")
        else:
            name_keys.add(h)
            display = display or brands.get(h, "")
    if not display:
        return {}
    candidates: list[tuple[int, str]] = []
    for root in [r for r in (roots or []) if r]:
        for path, node in index.by_path.items():
            if not path or node.depth > MAX_DEPTH or is_garbled(node.name):
                continue
            if not (path == root or path.startswith(root + "/")):
                continue
            toks = tokens(node.name)
            keys = {stem(t) for t in toks} | set(toks)
            if keys & name_keys:
                candidates.append((node.depth, path))
    homes: list[str] = []
    for _, path in sorted(candidates):     # верхние папки проекта — канонические дома
        if path not in homes:
            homes.append(path)
        if len(homes) >= 3:
            break
    personal_root = str(config.get("routing", {}).get("personal_root", "Личное - другие проекты"))
    fallback = "" if homes else f"{personal_root}/{display}"
    return {"brand": display, "homes": homes, "fallback": fallback}


def hint_text(info: dict, config: dict) -> str:
    """Текст подсказки оператора для промпта ('', если сигнал молчит)."""
    if not info:
        return ""
    brand = info.get("brand", "")
    homes = info.get("homes") or []
    if homes:
        listed = "; ".join(f"«{h}»" for h in homes)
        return (f"По пути/имени файл похож на материал проекта «{brand}». Его дом в структуре: {listed}. "
                "Если содержание этому соответствует, клади в ЭТОТ раздел (или его существующую подпапку), "
                "а не в общие папки отдела; если сомневаешься — оставь своё решение и поставь review=true.")
    fallback = info.get("fallback") or ""
    if fallback:
        return (f"По пути/имени файл — материал проекта «{brand}», но своей папки у него в структуре отдела "
                f"ещё нет. Если содержание точно его — предложи ОДНУ новую подпапку «{fallback}» "
                "(move_confidence не выше 0.80, review=true): оттуда владелец переложит на диск проекта. "
                "Другие разделы для этого материала не выдумывай.")
    return ""


def project_hint(rel: str, index, roots: list[str], config: dict) -> str:
    """Совместимость со старым вызовом: первая папка-дом по бренду, иначе ''."""
    return (project_homes(rel, index, roots, config).get("homes") or [""])[0]

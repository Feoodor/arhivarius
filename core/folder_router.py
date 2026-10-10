"""Контекстный проход «папки из Разобрать → постоянное место».

Отдельный от разбора файлов: модель получает НЕ текст каждого файла, а досье
папки — имя, список подпапок и имён файлов (и только при коллизии имён в корне —
выдержки из пары representative-файлов). Ответ — существующий раздел из скелета
карты, куда папку перенести целиком (или «разобрать по частям», или «не трогать»).

Промпт редактируется: prompt_folder.md рядом с программой (вкладка «Промпт»).
Решения модель только предлагает: перенос утверждается в окне «Папки».
"""
from __future__ import annotations

import json
from pathlib import Path

FOLDER_SYSTEM = (
    "Ты — архивариус «Молодёжный отдел». Разбирается ЦЕЛАЯ папка из "
    "зоны «Разобрать». По её имени, списку подпапок и файлов (текст файлов не читается) "
    "выбери, в какой раздел СУЩЕСТВУЮЩЕЙ структуры её положить целиком. Путь раздела бери "
    "дословно из КАРТЫ РАЗДЕЛОВ. Свалочные папки («Разное», «Прочее», «Новая папка») "
    "выбирать нельзя: подходящего места нет — верни section=\"\" и split=true. Если "
    "содержимое папки явно разношенное — split=true (её разберут по файлам обычным "
    "конвейером). Если надёжного места нет — section=\"\" и review=true. Не выдумывай "
    "разделы. Материал, названный по конкретному событию (музыка К БАЛУ, афиша слёта, "
    "программа фестиваля, год в названии), — это материал СОБЫТИЯ: выбери для него на "
    "карте форматную ветку мероприятий (например «Документы/Мероприятия/Балы» или "
    "«Документы/Мероприятия/Праздники»), а НЕ тематическое направление со похожим словом "
    "(«Песенно-музыкальное» и т.п.) — в «Направлениях…» живёт регулярная деятельность "
    "(репетиции, занятия, выпуски), а не сопровождение одного мероприятия. Точную папку "
    "события и год подберёт следующий проход — тебе достаточно формата."
)


FOLDER_USER_SAMPLE = """{sections_block}

ПАПКА ИЗ ЗОНЫ РАЗБОРА
Путь: {folder_rel}
Файлов внутри: {total}
Подпапки: {child_folders}
Файлы (первые {sample_n}): {child_files}
{collision_block}{context_block}
Верни JSON: section (пусто — если места нет), confidence, split, reason."""

FOLDER_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "description": "раздел из КАРТЫ РАЗДЕЛОВ дословно, "
                                                     "куда положить папку целиком; пусто — нет места"},
        "confidence": {"type": "number", "description": "0..1"},
        "split": {"type": "boolean", "description": "true — содержимое разношенное, разбирать по файлам"},
        "reason": {"type": "string", "description": "одна фраза: по каким названиям решили"},
    },
    "required": ["section", "confidence", "split", "reason"],
}


def _fold(value: str) -> str:
    return (value or "").casefold().replace("ё", "е")


def intake_top_folders(config: dict, snapshot: dict) -> list[str]:
    """Папки первого уровня внутри всех зон «Разобрать» из hide_prefixes."""
    roots = [str(p).replace("\\", "/").strip("/") for p in config.get("folders", {}).get("hide_prefixes", [])]
    out: set[str] = set()
    for item in snapshot.get("folders", []):
        rel = str(item).replace("\\", "/")
        for r in roots:
            if rel.startswith(r + "/"):
                tail = rel[len(r) + 1:].split("/")
                if len(tail) == 1 and tail[0]:
                    out.add(rel)
    return sorted(out)


def folder_dossier(config: dict, snapshot: dict, folder_rel: str, sample_files: int = 25) -> dict:
    pre = folder_rel.rstrip("/") + "/"
    total = 0
    children: list[str] = []
    subfolders: set[str] = set()
    for item in snapshot.get("files", []):
        p = str(item.get("path", "")).replace("\\", "/")
        if p.startswith(pre):
            total += 1
            rest = p[len(pre):]
            if "/" not in rest:
                children.append(rest)
    for f in snapshot.get("folders", []):
        p = str(f).replace("\\", "/")
        if p.startswith(pre) and p[len(pre):].strip("/"):
            subfolders.add(p[len(pre):].split("/")[0])
    return {"rel": folder_rel, "total": total,
            "child_folders": sorted(subfolders)[:20],
            "child_files": sorted(children)[:sample_files],
            "sample_n": min(sample_files, len(children))}


def root_name_collisions(config: dict, index, folder_rel: str, roots: list[str]) -> list[str]:
    """Одноимённые (норм) папки на глубине ≤2 от корней — повод заглянуть в текст."""
    name = folder_rel.rpartition("/")[2]
    key = _fold(name)
    hits = []
    for path, node in index.by_path.items():
        if not path or node.depth > 2 or not roots:
            continue
        if not any(path == r or path.startswith(r + "/") for r in roots):
            continue
        if _fold(node.name) == key and path != name and path.casefold() != folder_rel.casefold():
            hits.append(path)
    return sorted(hits, key=len)[:4]


def _context_snippets(config: dict, folder_rel: str, limit_files: int = 2, chars: int = 700) -> str:
    from . import extract as extract_mod
    root: Path = config["scan_root"]
    folder = root / folder_rel
    out = []
    try:
        entries = sorted([p for p in folder.iterdir() if p.is_file()], key=lambda p: p.name)
    except OSError:
        return ""
    picked = entries[:limit_files]
    for p in picked:
        if p.suffix.lower() not in {".docx", ".pdf", ".txt", ".md", ".xlsx", ".pptx", ".csv", ".html"}:
            continue
        try:
            data = extract_mod.extract(p, chars, 4, ocr=False, media_meta=False)
        except Exception:
            continue
        text = (data.text or "").strip()[:chars]
        if text:
            out.append(f"— {p.name}: {text}")
    return "\n".join(out)


def propose_folder(config: dict, dossier: dict, collisions: list[str], index, client,
                   app_dir: Path | None = None) -> dict:
    """Один запрос модели по досье папки. Возвращает {section, confidence, split, reason, note}."""
    from .prompts import _load_template, _render, section_schema

    full = bool(config.get("folders", {}).get("nav_full_map", True))
    map_header = "КАРТА РАЗДЕЛОВ (полный путь на строке):\n"
    if full:
        # вся карта деревом (~9k токенов): видны даже глубокие папки; enum на таком
        # списке только раздувает запрос — ответ проверяем валидацией ниже
        section_pack = index.full_map(
            budget_chars=int(config.get("folders", {}).get("nav_map_budget_chars", 30000)),
            skip_empty=True)
        schema = FOLDER_SCHEMA  # ключи фиксирует грамма́тика, enum на всю карту не нужен
        map_header = ("КАРТА (дерево; склеенные цепочки — это один путь через «/»; "
                      "отвечай полным путём от корня, дословно с карты):\n")
    else:
        prefix = str(config.get("mo_target_prefix", "")).strip("/")
        keep = index.keep_up_to(2)
        mezp = f"{prefix}/Мероприятия/" if prefix else "Мероприятия/"
        keep |= {p for p in index.by_path
                 if p.startswith(mezp) and p.count("/") == mezp.count("/")
                 and not index.excluded(p)}
        section_pack = index.pack_from_keep(keep, True, 14000, flat=True)
        schema = section_schema(section_pack.paths_list)
    template = _load_template(Path(app_dir or config.get("app_dir") or Path.cwd()),
                              "prompt_folder.md", FOLDER_USER_SAMPLE)
    collision_block = ""
    context_block = ""
    if collisions:
        collision_block = ("В структуре уже есть одноимённые папки: "
                           + "; ".join(collisions) + ". Реши, в какую из них (или общую) положить.\n")
        snippets = _context_snippets(config, dossier["rel"])
        if snippets:
            context_block = "ВЫДЕРЖКИ ИЗ ФАЙЛОВ ПАПКИ (для различения):\n" + snippets + "\n"
    user = _render(template, {
        "sections_block": map_header + section_pack.text,
        "folder_rel": dossier["rel"],
        "total": dossier["total"],
        "child_folders": ", ".join(dossier["child_folders"]) or "(нет)",
        "child_files": ", ".join(dossier["child_files"]) or "(нет)",
        "sample_n": dossier["sample_n"],
        "collision_block": collision_block,
        "context_block": context_block,
    })
    allowed = set(section_pack.paths_list)
    try:
        result = client.ask(FOLDER_SYSTEM, user, schema)
    except Exception as exc:  # сбой не роняет проход — строка с note уйдёт на решение человека
        return {"section": "", "confidence": 0.0, "split": False,
                "reason": "", "note": f"сбой модели: {str(exc)[:120]}"}
    data = result.data or {}
    section = str(data.get("section", "")).replace("\\", "/").strip().strip("/")
    if section and section not in allowed:
        folded = {_fold(p): p for p in allowed}
        section = folded.get(_fold(section), "")
    if section and section not in allowed:
        tail = "/" + _fold(section)
        suff = [s for s in allowed if _fold(s).endswith(tail)]
        if len(suff) == 1:
            section = suff[0]
    if section and section not in allowed:
        # модель могла собрать путь из цепочки дерева с мелкой ошибкой или снять
        # корень — прощаем написание, пробуем с префиксом, принимаем только
        # реально существующую папку
        from .rules import Verdict, resolve_path
        prefix = str(config.get("mo_target_prefix", "")).strip("/")
        for cand_val in [section] + ([f"{prefix}/{section}"] if prefix else []):
            cand = resolve_path(cand_val, index, Verdict())
            if cand in allowed:
                section = cand
                break
        else:
            section = ""
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"section": section, "confidence": round(conf, 2),
            "split": bool(data.get("split")), "reason": str(data.get("reason", ""))[:300],
            "collisions": collisions, "note": ""}


def propose_all(config: dict, log=None, limit: int = 30) -> list[dict]:
    """Пройтись по папкам верхнего уровня всех зон «Разобрать»; сохранить план."""
    from . import pipeline
    from .llm import OllamaClient
    from .folders import curated_root_sections

    log = log or (lambda m: None)
    llm_cfg = config.get("llm", {})
    snapshot, index = pipeline._snapshot_and_index(config)
    roots = list(index.top_levels)
    config["top_level_roots"] = curated_root_sections(index, config)
    client = OllamaClient(
        base_url=llm_cfg.get("base_url", "http://127.0.0.1:11434"),
        model=llm_cfg.get("model_text", "gemma3:12b"),
        num_ctx=min(int(llm_cfg.get("num_ctx", 16384)), 20480),
        num_predict=260, temperature=0.1, top_p=0.9,
        keep_alive=llm_cfg.get("keep_alive", "30m"),
        timeout=int(llm_cfg.get("timeout_seconds", 900)),
        cache_policy=llm_cfg.get("cache_policy", "nonce"),
        structured=True, retries=1)
    old_plan = {row.get("откуда"): row for row in load_plan_safe(config)}
    folders = intake_top_folders(config, snapshot)[: max(1, int(limit))]
    rows: list[dict] = []
    for n, folder in enumerate(folders, 1):
        dossier = folder_dossier(config, snapshot, folder)
        collisions = root_name_collisions(config, index, folder, roots)
        log(f"  папка {n}/{len(folders)}: {folder}")
        answer = propose_folder(config, dossier, collisions, index, client,
                                Path(config.get("app_dir", Path.cwd())))
        prev = old_plan.get(folder, {})
        row = {"откуда": folder, "section": answer["section"], "split": answer["split"],
               "confidence": answer["confidence"], "reason": answer["reason"],
               "collisions": answer.get("collisions", []), "note": answer.get("note", ""),
               "решение": prev.get("решение", ""), "файлов": dossier["total"]}
        rows.append(row)
    from . import folders_ops
    folders_ops.save_plan(config, rows)
    return rows


def load_plan_safe(config: dict) -> list[dict]:
    try:
        path = Path(config["output_path"]) / "folder_plan.json"
        return list(json.loads(path.read_text("utf-8"))) if path.exists() else []
    except Exception:
        return []

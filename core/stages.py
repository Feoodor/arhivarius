"""Постепенное уточнение файла: сначала факты → потом имя → потом место.

Идея (просьба Фёдора «модель уточняла данные постепенно»): вместо одного большого
ответа разбираем файл в три коротких шага, каждый опирается на предыдущий:

  1) ФАКТЫ  — только читаем содержимое и выписываем: тип, даты, событие, участников,
              темы, это шаблон/скан/входящее/исходящее, номер. Никаких решений.
  2) ИМЯ    — из фактов и формата имён prompt.md собираем «ГГГГ-ММ-ДД_Тип_Тема.ext».
  3) МЕСТО  — из фактов + имени + КАРТЫ папок + соседей выбираем СУЩЕСТВУЮЩУЮ папку;
              новую придумываем в крайнем случае и тогда снижаем move_confidence.

Общий системный блок — build_system(config), то есть те же правила именования и
разделов, что и в основном конвейере (единственный источник истины — prompt.md).
На каждом шаге явно просим маленький JSON; format=json мягче полной схемы, поэтому
шаги реже «пустят» (не возвращают ничего).

Итог склеивается в ту же форму, что core.refine.refine(), — карта и просмотрщик
не меняются. Каждый шаг застрахован: сорвётся один — конвейер доделает по тому,
что есть, плюс прежнее предложение. Ничего не переносится молча: решение за человеком.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def _conf(val, default: float = 0.0) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return round(min(max(f, 0.0), 1.0), 2)


def refine_staged(config: dict, rel_path: str, previous: dict | None = None,
                  comment: str = "", log=None) -> dict:
    """Три шага (факты → имя → место). Возвращает тот же dict, что refine.refine()."""
    from .llm import OllamaClient
    from .pipeline import context_pack_for, _snapshot_and_index
    from .prompts import build_system, DOC_TYPES
    from .folders import curated_root_sections
    from .config_loader import CHARS_PER_TOKEN, ANSWER_RESERVE_TOKENS
    from .refine import _listing, _maybe_image

    log = log or (lambda msg: None)
    rel = rel_path.replace("\\", "/")
    previous = previous or {}
    llm_cfg = config.get("llm", {})
    content_cap = int(llm_cfg.get("content_chars", 9000))

    # Индекс папок — чтобы детерминированно отличить существующую папку от выдуманной.
    try:
        _snap, index = _snapshot_and_index(config)
        config["top_level_roots"] = curated_root_sections(index, config)
    except Exception:  # снимка нет — место предложим без строгой проверки на вымысел
        index = None
    system = build_system(config)

    # Картинку/PDF-скан — зрению на её (меньшем) окне; иначе — текстовой модели.
    images = _maybe_image(config, rel, None)
    if images:
        model = llm_cfg.get("model_vision", llm_cfg.get("model_text", "gemma3:12b"))
        eff_ctx = min(int(llm_cfg.get("num_ctx", 16384)), int(llm_cfg.get("vision_num_ctx", 16384)))
    else:
        model = llm_cfg.get("model_text", "gemma3:12b")
        eff_ctx = int(llm_cfg.get("num_ctx", 16384))

    # Безопасный бюджет карты: инструкция + текст файла должны влезть в окно запроса.
    used = (len(system) + content_cap) / CHARS_PER_TOKEN + ANSWER_RESERVE_TOKENS
    map_budget = int(max(4000, (eff_ctx - used) * CHARS_PER_TOKEN))
    map_budget = min(map_budget, int(config.get("folders", {}).get("budget_chars", 22000)))
    item, data, pack, _idx = context_pack_for(config, rel, budget_chars=map_budget)

    neigh_current = _listing(config, rel.rpartition("/")[0])
    prev_target = str(previous.get("Целевой путь", "") or "")
    neigh_target = _listing(config, prev_target) if prev_target else ""
    content = (data.text or "(текст не извлечён)")[:content_cap]
    filename = Path(rel).name

    def client(predict: int) -> OllamaClient:
        return OllamaClient(
            base_url=llm_cfg.get("base_url", "http://127.0.0.1:11434"),
            model=model, num_ctx=eff_ctx, num_predict=predict, temperature=0.15,
            top_p=float(llm_cfg.get("top_p", 0.9)), keep_alive=llm_cfg.get("keep_alive", "30m"),
            timeout=int(llm_cfg.get("timeout_seconds", 900)),
            cache_policy=llm_cfg.get("cache_policy", "nonce"), structured=False, retries=1,
        )

    def ask_json(predict: int, user: str, imgs=None) -> dict:
        res = client(predict).ask(system, user, images=imgs)
        return res.data or {}

    started = datetime.now()
    total_secs = 0.0
    steps: dict[str, dict] = {}

    # ------------------------------------------------------- ШАГ 1. ФАКТЫ
    s1 = (
        "ШАГ 1 из 3 — только ПРОЧТИ и выпиши ФАКТЫ. Ничего не предлагай, не переноси, "
        "не переименовывай.\n\n"
        f"Файл: {rel}\n"
        f"Размер: {int((item or {}).get('size') or 0)} байт · Содержимое: {data.kind}"
        f"{' · ' + data.note if data.note else ''}\n"
        f"КОММЕНТАРИЙ ЧЕЛОВЕКА (важно, если есть): {comment.strip() or '(нет)'}\n\n"
        f"СОДЕРЖИМОЕ:\n{content}\n\n"
        "Верни СТРОГО один JSON без пояснений вида:\n"
        '{"doc_type":"...", "date_doc":"ГГГГ-ММ-ДД или пусто", '
        '"event":"название события или пусто", "event_start":"...", "event_end":"...", '
        '"participants":"адресат/отправитель/кто участвует, кратко", '
        '"topics":"о чём 3–6 слов", "number":"Вх-NNN/Исх-NNN или пусто", '
        '"is_template":false, "is_scan":false, "incoming":false, "outgoing":false, '
        '"uncertain":"что неясно или пусто"}\n'
        f"doc_type только из: {DOC_TYPES}\n"
        "Дату пиши ровно тем уровнем, каким она названа: только год → «ГГГГ», год+месяц → "
        "«ГГГГ-ММ», полная дата → «ГГГГ-ММ-ДД». НЕ подставляй первое число/месяц, если их "
        "нет в тексте; неизвестное поле оставь пустым."
    )
    facts: dict = {}
    try:
        facts = ask_json(500, s1, imgs=images)
        if not isinstance(facts, dict):
            facts = {}
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ шаг «факты» сорвался: {type(exc).__name__}: {str(exc)[:120]}")
    steps["facts"] = facts
    facts_line = ", ".join(f"{k}={v}" for k, v in facts.items()
                           if v not in (None, "", False, "пусто")) or "(факты не выписаны)"

    # ------------------------------------------------------- ШАГ 2. ИМЯ
    s2 = (
        "ШАГ 2 из 3 — СОБЕРИ ИМЯ файла по формату из инструкции.\n\n"
        f"Текущее имя: {filename}\n"
        f"ФАКТЫ из шага 1: {facts_line}\n"
        f"СОДЕРЖИМОЕ (для сверки):\n{content[:1800]}\n\n"
        "Правила: дата — по правилам Д1/Д2; тип — из фактов; блоки через «_», слова "
        "через «-»; расширение сохрани как у текущего файла; бессмысленное имя замени "
        "смыслом; если имя уже по формату и верное — оставь его. Не выдумывай номер "
        "Вх/Исх, если его нет в тексте. Дату в имени бери тем же уровнем, что в фактах: "
        "если в ФАКТАХ год только «ГГГГ» (без месяца) — начинай имя с «ГГГГ_» и НЕ "
        "дополняй месяц/день.\n\n"
        "Верни СТРОГО один JSON:\n"
        '{"new_name":"…расш", "name_confidence":0.0..1.0, "why":"1–2 фразы почему так"}'
    )
    name_out: dict = {}
    try:
        name_out = ask_json(360, s2, imgs=None)
        if not isinstance(name_out, dict):
            name_out = {}
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ шаг «имя» сорвался: {type(exc).__name__}: {str(exc)[:120]}")
    steps["name"] = name_out
    new_name = re_clean_name(str(name_out.get("new_name", "") or ""), filename)
    name_conf = _conf(name_out.get("name_confidence"), 0.0)

    # ------------------------------------------------------- ШАГ 3. МЕСТО
    s3 = (
        "ШАГ 3 из 3 — ВЫБЕРИ ПАПКУ (место), где этому файлу быть постоянно.\n\n"
        f"Имя из шага 2: {new_name}\n"
        f"ФАКТЫ: {facts_line}\n"
        f"Текущая папка: {rel.rpartition('/')[0] or '(корень)'}\n"
        f"Прежнее предложение папки: {prev_target or '(нет)'}\n\n"
        f"СОСЕДИ В ТЕКУЩЕЙ ПАПКЕ:\n{neigh_current or '(нет)'}\n"
        f"СОСЕДИ В ПРЕДЛОЖЕННОЙ ПАПКЕ:\n{neigh_target or '(нет)'}\n\n"
        f"КАРТА ПАПок (отсюда бери место ДОСЛОВНО; новую ветку придумывай в крайнем "
        f"случае и только одним уровнем внутри существующей):\n{pack.text or '(нет веток)'}\n\n"
        "Сначала ищи СУЩЕСТВУЮЩУЮ папку под этот материал; ориентируйся на событие/тему "
        "и на то, где лежат похожие файлы. Год папки события — по дню начала (Д2).\n\n"
        "Верни СТРОГО один JSON:\n"
        '{"target_path":"Документы/…/папка", "move_confidence":0.0..1.0, '
        '"invented":false, "why":"2–3 рассмотренных кандидата и почему выбран этот"}'
    )
    place_out: dict = {}
    try:
        place_out = ask_json(520, s3, imgs=None)
        if not isinstance(place_out, dict):
            place_out = {}
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ шаг «место» сорвался: {type(exc).__name__}: {str(exc)[:120]}")
    steps["place"] = place_out

    total_secs = round((datetime.now() - started).total_seconds(), 1)
    return _merge(facts, new_name, name_conf, place_out, filename=filename,
                  prev_target=prev_target, cur_dir=rel.rpartition("/")[0], index=index,
                  seconds=total_secs, model=model, steps=steps)


def _merge(facts: dict, new_name: str, name_conf: float, place_out: dict, *,
           filename: str, prev_target: str, cur_dir: str, index,
           seconds: float = 0.0, model: str = "", steps: dict | None = None) -> dict:
    """Чистая сборка ответа из трёх шагов (без обращений к модели) — чтобы можно было
    прогнать на фикстурах в selftest. Кап «выдуманной» папки и запасные значения здесь."""
    steps = steps or {}
    if not new_name:
        new_name = filename
    target = str(place_out.get("target_path", "") or "").strip().strip("/")
    move_conf = _conf(place_out.get("move_confidence"), 0.0)
    if not target:
        target = prev_target or cur_dir

    # Детерминированная страховка: выдуманная папка не может быть «уверенной».
    invented = bool(place_out.get("invented"))
    if index is not None and target and target not in index.by_path:
        invented = True
    if invented:
        move_conf = min(move_conf, 0.80) if move_conf else 0.60

    reason = "постепенно → " + " · ".join(
        filter(None, [
            "имя: " + str(steps.get("name", {}).get("why", ""))[:150],
            "место: " + str(place_out.get("why", ""))[:170],
            ("папка новая" if invented else ""),
        ]))[:400]

    return {
        "new_name": new_name,
        "target_path": target,
        "name_confidence": name_conf,
        "move_confidence": move_conf,
        "confidence": round(min(name_conf, move_conf), 2) if (name_conf or move_conf) else 0.0,
        "reason": reason or "постепенный разбор не дал ответа",
        "review": True,
        "doc_type": str(facts.get("doc_type", "") or ""),
        "seconds": seconds,
        "model": model,
        "staged": True,
        "invented": invented,
        "steps": steps,
    }


def re_clean_name(name: str, original: str) -> str:
    """Имя из модели: убрать мусор, вернуть расширение исходника, если модель его съела."""
    import re
    name = (name or "").strip().strip("/")
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    orig_ext = Path(original).suffix.lower()
    if name and orig_ext and Path(name).suffix.lower() != orig_ext:
        name = name + orig_ext
    return name

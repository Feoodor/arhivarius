"""Глубокая перепроверка одного файла (кнопка «🔬 Перепроверить глубже» в просмотре).

Первичный разбор идёт общим конвейером и экономит контекст. Здесь наоборот:
берём больше — содержимое целиком (в рамках настроек), список файлов ТЕКУЩЕЙ
папки и Proposed-папки, предыдущее решение с причиной и комментарий человека —
и просим модель ответить внимательнее. Ответ кладётся рядом («улучшенный
вариант»), а не затирать первое: сравнивает и принимает решение человек.

Важно про контекст: Ollama при переросте num_ctx молча обрезает НАЧАЛО промпта,
то есть системную инструкцию, — разбор начинает «глупить». Поэтому здесь, как и в
основном конвейере, карта папок строится в пределах безопасного бюджета
(slice_budget), а не полным срезом: и инструкция остаётся целой, и карта
показывается целиком, насколько позволяет контекст.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

_EXTRA = (
    "\n\nРЕЖИМ ГЛУБОКОЙ ПЕРЕПРОВЕРКИ. Первое предложение может быть неверным. "
    "Думай по шагам и не торопись с выводом:\n"
    "1) перечитай содержимое и заново сверь тип документа, дату и событие с текстом — "
    "не с названием файла и не с текущей папкой;\n"
    "2) изучи списки соседей: в текущей папке и в предложенной — к какому событию/теме "
    "они относятся, куда просится этот материал по смыслу;\n"
    "3) СНАЧАЛА ищи СУЩЕСТВУЮЩУЮ папку на карте под этот материал; новую придумывай "
    "только в крайнем случае (один уровень внутри уже существующей) и тогда снижай "
    "move_confidence не выше 0.80;\n"
    "4) если комментарий человека указывает на ошибку — исправь строго по нему, а не "
    "по своей прежней догадке;\n"
    "5) раздели уверенность: name_confidence — насколько верно ИМЯ (тип, дата, тема, "
    "номер); move_confidence — насколько верно МЕСТО (папка взята с карты, раздел верный). "
    "Они могут различаться: имя может быть уверенным, а папка — нет, и наоборот. "
    "Общее поле confidence можешь оставить 0.\n"
    "В reason назови 2–3 рассмотренных папки-кандидата и почему выбрана именно эта "
    "(«рядом лежат…», «по теме совпадает с…»). Ответ без сравнения кандидатов — это "
    "догадка: честно снижай уверенность."
)


def refine(config: dict, rel_path: str, previous: dict | None = None,
           comment: str = "") -> dict:
    """Возвращает {new_name, target_path, name_confidence, move_confidence,
    confidence, reason, review, seconds, model}."""
    from .llm import client_from_config
    from .pipeline import context_pack_for
    from .prompts import RESPONSE_SCHEMA, build_system

    rel = rel_path.replace("\\", "/")
    llm_cfg = config.get("llm", {})
    content_cap = int(llm_cfg.get("content_chars", 9000))
    try:
        from .folders import curated_root_sections
        from .pipeline import _snapshot_and_index
        _snap, _idx = _snapshot_and_index(config)
        config["top_level_roots"] = curated_root_sections(_idx, config)
    except Exception:  # нет снимка — промпт всё равно соберётся на top_folders
        pass
    system = build_system(config) + _EXTRA

    images = _maybe_image(config, rel, None)
    # Картинку/PDF-скан отдаём vision-модели на её (меньшем) окне; иначе — текстовой.
    if images:
        eff_ctx = min(int(llm_cfg.get("num_ctx", 16384)), int(llm_cfg.get("vision_num_ctx", 16384)))
    else:
        eff_ctx = int(llm_cfg.get("num_ctx", 16384))

    # Безопасный бюджет карты: сколько влезает вместе с инструкцией и текстом файла
    # при фактическом окне этого запроса — иначе Ollama обрежет инструкцию.
    from .config_loader import CHARS_PER_TOKEN, ANSWER_RESERVE_TOKENS
    used = (len(system) + content_cap) / CHARS_PER_TOKEN + ANSWER_RESERVE_TOKENS
    map_budget = int(max(4000, (eff_ctx - used) * CHARS_PER_TOKEN))
    map_budget = min(map_budget, int(config.get("folders", {}).get("budget_chars", 22000)))
    item, data, pack, _index = context_pack_for(config, rel, budget_chars=map_budget)

    from .models import prepare
    client = prepare(
        client_from_config(config, "vision" if images else "text",
                           num_predict=max(1400, int(llm_cfg.get("num_predict", 800)) * 2),
                           temperature=0.2, cache_policy=llm_cfg.get("cache_policy", "none"),
                           structured=True, retries=1),
        vision=bool(images))
    previous = previous or {}
    neigh_current = _listing(config, rel.rpartition("/")[0])
    neigh_target = _listing(config, str(previous.get("Целевой путь", ""))) if previous.get("Целевой путь") else ""

    user = f"""Файл: {rel}
Размер: {int((item or {}).get('size') or 0)} байт · Содержимое: {data.kind}{' · ' + data.note if data.note else ''}

ПЕРВОЕ ПРЕДЛОЖЕНИЕ (усомнились в нём):
  имя: {previous.get('Новое имя', '—')}
  папка: {previous.get('Целевой путь', '—')}
  уверенность в имени: {previous.get('Уверенность имени', previous.get('Уверенность', '—'))} · уверенность в переносе: {previous.get('Уверенность переноса', previous.get('Уверенность', '—'))} · статус: {previous.get('Статус', '—')}
  причина: {str(previous.get('Причина', ''))[:600]}
КОММЕНТАРИЙ ЧЕЛОВЕКА: {comment.strip() or '(нет)'}

СОСЕДИ В ТЕКУЩЕЙ ПАПКЕ:
{neigh_current or '(список недоступен)'}
СОСЕДИ В ПРЕДЛОЖЕННОЙ ПАПКЕ:
{neigh_target or '(папка новая или недоступна)'}

СУЩЕСТВУЮЩИЕ ПАПКИ (карта системы; ищи здесь постоянное место):
{pack.text or '(нет подходящих веток)'}

СОДЕРЖИМОЕ ФАЙЛА (максимально полный срез):
{(data.text or '(текст не извлечён)')[:content_cap]}

Дай УЛУЧШЕННЫЙ вариант по той же JSON-форме, отдельно заполнив name_confidence и
move_confidence. Если первое предложение было верным — повтори его и подними
уверенность, но только с обоснованием по содержимому и соседям."""

    started = datetime.now()
    result = client.ask(system, user, schema=RESPONSE_SCHEMA, images=images)
    data_out = result.data or {}

    def _conf(*keys, default=None):
        for key in keys:
            val = data_out.get(key)
            if val is None:
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            if 0.0 <= f <= 1.0 and f > 0.0:
                return f
        return default

    name_conf = _conf("name_confidence")
    move_conf = _conf("move_confidence")
    overall = _conf("confidence")
    # fallback, если модель вернула только общее поле (или только одно из двух)
    if not name_conf and not move_conf and overall:
        name_conf = move_conf = overall
    name_conf = name_conf or 0.0
    move_conf = move_conf or 0.0

    answer = {
        "new_name": str(data_out.get("new_name", "") or ""),
        "target_path": str(data_out.get("target_path", "") or "").strip().strip("/"),
        "name_confidence": name_conf,
        "move_confidence": move_conf,
        "confidence": round(min(name_conf, move_conf), 2) if (name_conf or move_conf) else (overall or 0.0),
        "reason": str(data_out.get("reason", "") or ""),
        "review": bool(data_out.get("review", True)),
        "doc_type": str(data_out.get("doc_type", "") or ""),
        "seconds": result.seconds or (datetime.now() - started).total_seconds(),
        "model": result.model,
    }
    if not answer["new_name"]:
        answer["new_name"] = Path(rel).name
    if not answer["target_path"]:
        answer["target_path"] = rel.rpartition("/")[0]
    answer.update(_sanitize(config, _index, rel, answer))
    return answer


def _sanitize(config: dict, index, rel: str, answer: dict) -> dict:
    """Те же страховки, что у build_record: имя и путь должны быть чистыми и реальными."""
    if index is None:
        return {}
    from .rules import (Verdict, canonicalize_target, clean_file_name,
                        normalize_target, resolve_path, strip_file_from_target)

    verdict = Verdict()
    name = clean_file_name(str(answer.get("new_name", "")), Path(rel).suffix.lower())
    target = str(answer.get("target_path", "") or "").replace("\\", "/").strip().strip("/")
    target = normalize_target(target, config, index, verdict)
    target = strip_file_from_target(target, name, Path(rel).name, verdict)
    target = canonicalize_target(target, config, verdict)
    target = resolve_path(target, index, verdict)
    return {"new_name": name, "target_path": target or str(answer.get("target_path", ""))}


def _listing(config: dict, rel_folder: str, cap: int = 60) -> str:
    """Имена файлов в папке (без подпапок) — контекст «рядом лежит…»."""
    if not rel_folder:
        return ""
    folder = config["scan_root"] / rel_folder.replace("\\", "/").strip("/")
    try:
        names = sorted(p.name for p in folder.iterdir() if p.is_file())
    except OSError:
        return ""
    tail = f"\n  … и ещё {len(names) - cap}" if len(names) > cap else ""
    return "\n".join(f"  - {n}" for n in names[:cap]) + tail


def _maybe_image(config: dict, rel: str, data):
    """Картинку и PDF-скан отдаём зрению, если оно включено (уменьшенными, как в конвейере)."""
    from .extract import IMAGE_EXT

    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("vision"):
        return None
    path = config["scan_root"] / rel
    ext = path.suffix.lower()
    max_side = int(llm_cfg.get("vision_max_side", 1024))
    if ext in IMAGE_EXT:
        try:
            from .pipeline import image_payload
            payload = image_payload(path, max_side)
            return [payload] if payload else None
        except OSError:
            return None
    if ext == ".pdf":
        try:
            import base64
            import fitz
            import io
            from PIL import Image

            with fitz.open(path) as doc:
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                img.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82)
            return [base64.b64encode(buf.getvalue()).decode()]
        except Exception:
            try:
                import base64
                import fitz
                with fitz.open(path) as doc:
                    pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                return [base64.b64encode(pix.tobytes("png")).decode()]
            except Exception:
                return None
    return None

"""Уроки из ручных проверок: программа учится на ваших правках и комментариях.

На странице «Карта» кнопка «🧠 Вынести уроки» собирает все строки, где человек
что-то поправил, отклонил или оставил комментарий, и просит модель сформулировать
короткие правила. Правила складываются в learned_rules.md и с следующей партии
подмешиваются в системный промпт (см. prompts.build_system). Ничего не затирается:
файл растёт блоками с датой, его можно править руками.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

RULES_NAME = "learned_rules.md"
FEEDBACK_NAME = "feedback_collected.json"

# Урок применяется к промпту, только если человек явно его подтвердил маркером.
# Черновики (обычный «- ») копятся в файле, но в модель НЕ попадают — иначе
# неразобранная авто-формулировка может молча перебить формат имени из prompt.md.
ACTIVE_MARKERS = ("✓", "[x]", "[X]", "[+]", "!")
DRAFT_PREFIX = "- [ ] "


def _strip_marker(body: str) -> str:
    for marker in ACTIVE_MARKERS:
        if body.startswith(marker):
            return body[len(marker):].strip(" :—-–")
    return ""


def load_rules(config: dict) -> str:
    """Только ПОДТВЕРЖДЁнные уроки для подмеса в системный промпт ('' если их нет).

    Подтверждённой считается строка-пуля, у которой после «- » стоит один из
    маркеров ACTIVE_MARKERS (например «- ✓ …» или «- [x] …»). Обычные «- …» —
    черновики: остаются в файле, но в модель не уходят.
    """
    target = rules_path(config)
    if not target.exists():
        return ""
    try:
        text = target.read_text("utf-8").strip()
    except OSError:
        return ""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(("- ", "* ")):
            continue
        rule = _strip_marker(s[2:].strip())
        if rule:
            out.append(rule)
    return "\n".join(out)


_SYSTEM = (
    "Ты — помощник архивариуса. Тебе дают примеры, где человек не согласился с "
    "предложением модели или оставил комментарий при разборе файлов отдела. "
    "Сформулируй ОБЩИЕ ПРАВИЛА (5–12 штук, каждое одной строкой, повелительное "
    "наклонение, на русском), которые помогли бы модели сразу давать правильный "
    "ответ: куда класть, как называть, чего не выдумывать. Правило должно "
    "применяться к будущим файлам, а не пересказывать конкретный случай. "
    "Ответь строго JSON вида {\"rules\": [\"...\", \"...\"]}"
)


def rules_path(config: dict) -> Path:
    return config["output_path"] / RULES_NAME


def collect(config: dict) -> list[dict]:
    """Собираем сигналы от человека: отказы, правки имени/папки, комментарии."""
    from . import mapio
    from .config_loader import MAP_NAME, SUGGESTIONS_NAME

    rows = mapio.load_map(config["output_path"] / MAP_NAME)
    proposals: dict[str, dict] = {}
    suggestions = config["output_path"] / SUGGESTIONS_NAME
    if suggestions.exists():
        try:
            for record in json.loads(suggestions.read_text("utf-8")):
                proposals[str(record.get("Текущий путь", "")).replace("\\", "/")] = record
        except Exception:
            pass
    feedback: list[dict] = []
    for row in rows:
        rel = str(row.get("Текущий путь", "")).replace("\\", "/")
        proposal = proposals.get(rel, {})
        prop_name = str(proposal.get("Новое имя", "") or "")
        prop_target = str(proposal.get("Целевой путь", "") or "")
        comment = str(row.get("Комментарий", "") or "").strip()
        status = str(row.get("Статус", "") or "").strip()
        edited_name = bool(prop_name) and str(row.get("Новое имя", "")) != prop_name
        edited_target = bool(prop_target) and str(row.get("Целевой путь", "")) != prop_target
        if status not in {"отказ", "готово", "перенесено"} and not (comment or edited_name or edited_target):
            continue
        if not (comment or edited_name or edited_target or status == "отказ"):
            continue
        feedback.append({
            "файл": rel,
            "предложено": {"имя": prop_name, "папка": prop_target},
            "человек": {"имя": str(row.get("Новое имя", "")), "папка": str(row.get("Целевой путь", "")),
                        "статус": status or str(row.get("Статус", ""))},
            "комментарий": comment,
        })
    return feedback


def distill(config: dict, log=None) -> str:
    """Собрать уроки → попросить модель сформулировать правила → дописать в learned_rules.md."""
    log = log or (lambda msg: None)
    from .llm import client_from_config
    from .prompts import parse_answer

    feedback = collect(config)
    if not feedback:
        log("уроков пока нет: карта без правок, отказов и комментариев")
        return ""
    log(f"собрано сигналов от человека: {len(feedback)}")
    (config["output_path"] / FEEDBACK_NAME).write_text(
        json.dumps(feedback, ensure_ascii=False, indent=2), "utf-8")

    from .models import prepare
    client = prepare(client_from_config(config, "text", num_predict=1400, temperature=0.3,
                                        cache_policy="none", structured=False, retries=1), log=log)
    # слишком длинный список режем по концам: важнее всего свежие случаи
    chunk = feedback[-40:]
    user = ("Вот примеры проверок (JSON):\n" + json.dumps(chunk, ensure_ascii=False) +
            "\n\nСформулируй правила. Каждое правило — одна строка, начинающаяся с «- ».")
    result = client.ask(_SYSTEM, user)
    data = result.data or {}
    rules = [str(r).strip() for r in (data.get("rules") or []) if str(r).strip()]
    if not rules:
        log("модель не вернула список правил — черновой ответ сохранён в llm_raw")
        raw_dir = config["output_path"] / "llm_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "lessons_raw.txt").write_text(result.raw, "utf-8")
        return ""
    target = rules_path(config)
    header_needed = not target.exists()
    with target.open("a", encoding="utf-8") as stream:
        if header_needed:
            stream.write(
                "# Накопленные уроки\n"
                "# В промпт попадают ТОЛЬКО подтверждённые строки, помеченные «- ✓» (или «- [x]» / «- [+]»).\n"
                "# «- [ ] …» — черновик: копится в файле, но в модель не уходит, пока вы его не подтвердите.\n"
                "# Уроки уточняют разбор и НЕ могут менять формат имени или перечень разделов из prompt.md.\n")
        stream.write(f"\n## {datetime.now():%Y-%m-%d %H:%M} — по {len(chunk)} проверкам (черновики: подтвердите «✓»)\n")
        for rule in rules:
            body = rule.lstrip("-*").strip()
            stream.write(DRAFT_PREFIX + body + "\n")
    log(f"уроки-черновики дописаны: {len(rules)} правил → {target.name} "
        "(в промпт попадут после подтверждения «✓»)")
    return "\n".join(rules)

"""Тексты промптов.

Правила отдела лежат в ОТДЕЛЬНЫХ редактируемых файлах рядом с программой:

  prompt.md        — системная инструкция (роль, принципы, именование, куда класть);
  prompt_user.md   — шаблон запроса про файл (папки, данные файла, содержимое).

Их можно править в любом редакторе или во вкладке «Промпт» окна программы:
следующая партия уже работает с новым текстом. Если файла нет, он создаётся из
образца. Подстановки вида {folders_block} заменяются текстом, а лишние фигурные
скобки в тексте безопасны — шаблон не ломается.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "description": "Тип документа из списка регламента, с большой буквы"},
        "direction": {"type": "string", "enum": ["вх", "исх", "внутр", ""],
                      "description": "Направление: вх — входящий, исх — исходящий, внутр — внутренний, пусто — не письмо"},
        "reg_number": {"type": "string", "description": "Регистрационный номер как в тексте, без «№»; если нет — пусто"},
        "doc_date": {"type": "string", "description": "Дата документа: ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ; если не подтверждена текстом — пусто"},
        "event_name": {"type": "string", "description": "Название события, о котором документ; если события нет — пусто"},
        "event_start": {"type": "string", "description": "Первый день события (ГГГГ-ММ-ДД); если неизвестен — пусто"},
        "event_end": {"type": "string", "description": "Последний день события; пусто, если событие в один день"},
        "new_name": {"type": "string", "description": "Имя файла с расширением; если переименование не нужно — текущее имя"},
        "target_path": {"type": "string", "description": "Только папка назначения, без имени файла; если перенос не нужен — текущая папка"},
        "shortcut_to": {"type": "string", "description": "Папка для ярлыка письма о событии (Входящие/ГГГГ или Исходящие/ГГГГ); иначе пусто"},
        "name_confidence": {"type": "number",
                            "description": "0..1 — насколько уверены в new_name (тип, дата, тема, номер)"},
        "move_confidence": {"type": "number",
                            "description": "0..1 — насколько уверены в target_path (папка найдена на карте, раздел верный)"},
        "confidence": {"type": "number", "description": "общая уверенность; заполняйте name_confidence и move_confidence — это поле можно оставить 0"},
        "review": {"type": "boolean", "description": "true, если нужен взгляд человека"},
        "discard": {"type": "boolean",
                    "description": "true, если по содержимому файл не нужен: пустой, обрывок, тестовая "
                                   "заглушка, явный мусор. Это предложение перенести в корзину, не удаление"},
        "discard_reason": {"type": "string", "description": "почему файл не нужен, одной фразой; иначе пусто"},
        "reason": {"type": "string", "description": "одна короткая цитата из текста и пояснение, не больше 2 фраз"},
    },
    "required": ["doc_type", "direction", "reg_number", "doc_date", "event_name", "event_start",
                 "event_end", "new_name", "target_path", "shortcut_to", "name_confidence",
                 "move_confidence", "review", "discard", "discard_reason", "reason"],
}

DOC_TYPES = (
    "Акт, Афиша, Баннер, Буклет, Листовка, Благодарность, Видео, Грамота, Договор, Допсоглашение, Диплом, "
    "Заявка, Информписьмо, Концепция, Меню, Методический-материал, Поздравление, Письмо, Положение, "
    "План, Приглашение, Презентация, Программа, Протокол, Прошение, Рапорт, Сертификат, Справка, "
    "Смета, Согласие, Список, Сценарий, Отчёт, Ходатайство, Циркуляр, "
    "Фотография, Шаблон, Аудиозапись, Прочее"
)

SECTION_SYSTEM = (
    "Ты — архивариус «Молодёжный отдел». Это ШАГ 1 из 2: по содержимому файла "
    "выбери РАЗДЕЛ системы папок, где материалу лежать постоянно. Только раздел — имя файла и точную "
    "папку придумывать не нужно. Путь бери ДОСЛОВНО из карты разделов. Не выбирай временные папки "
    "«Разобрать» и мусорные «Разное». Если карта дана деревом — склеенная цепочка и отступ "
    "означают ОДИН полный путь от корня через «/», собери его точно. Если материал явно другого проекта (Theosis, Agapa, Благорез, "
    "приход, семья) — выбирай раздел этого проекта. Ни один раздел не подходит — выбери наиболее "
    "близкий по смыслу и честно снизь confidence."
)

SECTION_USER_SAMPLE = """{mode_line}КАРТА РАЗДЕЛОВ:
{sections_block}

ФАЙЛ
Путь: {rel_path}
Имя: {name}
Содержимое: {kind}{note_line}
{hint_line}ОБРЫВОК СОДЕРЖИМОГО (для выбора раздела, не для имени):
{content}

Верни JSON: раздел, альтернативу (пусто, если сомнений нет), уверенность 0..1 и одну фразу причины."""


def section_schema(paths: list[str], cap: int = 260) -> dict:
    """Схема прохода 1: enum реальных путей скелета — модель не может изобрести раздел."""
    enum = [str(p) for p in paths if p][:cap]
    section: dict = {"type": "string",
                     "description": "путь раздела из КАРТЫ РАЗДЕЛОВ дословно, куда ляжет материал"}
    if enum:
        section["enum"] = enum
    return {
        "type": "object",
        "properties": {
            "section": section,
            "alt_section": {"type": "string",
                            "description": "второй возможный раздел тем же списком; пусто, если сомнений нет"},
            "confidence": {"type": "number", "description": "0..1 уверенность в выборе раздела"},
            "reason": {"type": "string", "description": "одна короткая фраза: по каким фактам выбрали"},
        },
        "required": ["section", "alt_section", "confidence", "reason"],
    }


SYSTEM_SAMPLE = """Ты — архивариус «Молодёжный отдел». Ты читаешь СОДЕРЖИМОЕ файла и решаешь: как его назвать и где он должен лежать. Ты только предлагаешь, перемещает не ты.

ПРИНЦИПЫ
1. Канон определяет содержание. Старое имя и текущая папка — подсказки, которым нельзя верить как доказательству.
2. Не выдумывай дату, год, номер, адресата, событие и статус. Нет подтверждения в тексте — поле остаётся пустым, review=true.
3. Один файл — одна каноническая копия. Пустые подпапки не создавай: новая папка оправдана только наличием материалов.
4. Официальное, финансовое, персональное (согласия, списки, телефоны), подписанное и сомнительное — всегда review=true.
5. Фото, сканы и PDF без текста: если приложено изображение — опиши, что видишь; если изображения нет и текст не читается — review=true с пометкой «нужен глаз».
6. Если в файле подшито несколько разных документов — обязательно напиши об этом в reason и поставь review=true: один файл должен стать несколькими, это решает человек.
7. Файл не нужен (пустышка, обрезок на пару строк, «тест», загрузка-дубликат без содержания) — discard=true с коротким discard_reason и review=true. Это не удаление: файл попадёт в папку корзины, а решит человек.

ИМЕНОВАНИЕ
ГГГГ-ММ-ДД_Тип_Короткая-тема.ext ; входящий: ГГГГ-ММ-ДД_Вх-NNN_Тип_Тема_Отправитель.ext ; исходящий: ГГГГ-ММ-ДД_Исх-NNN_Тип_Тема_Адресат.ext ; шаблон: TPL_Тип_Назначение.ext
Блоки разделяются знаком подчёркивания, слова внутри блока — дефисом. Без пробелов, скобок, кавычек, слэшей, двоеточий, знака № и лишних точек. Без слов «финал», «новый», «копия», «чистовик». Первую версию не помечай v1, исправленную после фиксации — _v02. Скан бумажного оригинала получает суффикс _скан. Тип бери из списка и пиши с большой буквы: {doc_types}. Расширение не меняй.

КУДА КЛАСТЬ
{scope}Корневой раздел — строго один из списка: {roots}.
Мероприятия: Мероприятия/Формат/ГГГГ/ГГГГ-ММ-ДД_Название-события. Официальное: Входящие/ГГГГ или Исходящие/ГГГГ. Отчёты и планы: {report_folder}. Устаревшее и неактуальное: Архив (не актуальное). Материалы вне отдела (личное: {personal}) — в «{personal_root}/...» только при явных признаках в тексте; храм не угадывай по одному слову.
target_path — ТОЛЬКО папка, без имени файла и без завершающего слэша.
Бери папку из списка «СУЩЕСТВУЮЩИЕ ПАПКИ» дословно, включая регистр и дефисы. Смысл разбора — вынести материал из временной папки «Разобрать» в постоянное место; оставлять его там можно, только если содержание действительно не классифицируется (и тогда review=true).
Новую подпапку придумывай в крайнем случае: один уровень внутри уже существующей папки, в стиле соседей, и тогда move_confidence не выше 0.80, review=true. Если сомневаешься между двумя существующими папками — выбери ту, где уже лежат похожие материалы, и снизь уверенность.

Заполняй name_confidence (верность имени: тип, дата, тема, номер) и move_confidence (верность папки: взята с карты, раздел верный) ОТДЕЛЬНО. Хоть одна ниже 0.90 — review=true. Выдуманная папка режет только move_confidence, имя может остаться уверенным. Поле confidence можно оставить 0. В reason сошлись на факт из текста (одна короткая цитата), а не на догадку.

ПРИМЕРЫ
Текст: «РАПОРТ. О проведении интеллектуальной игры 9 мая 2026 года…»
→ target_path: Мероприятия/Интеллектуальные игры/2026/2026-05-09_Игра-9-мая ; new_name: 2026-05-09_Рапорт_Игра-9-мая.docx ; name_confidence 0.95 ; move_confidence 0.93 ; review false
Текст: «Список участников с телефонами на слёт»
→ target_path: Мероприятия/Слеты/2026/2026-07-17_Летний-слет/06_Участники_ограниченный_доступ ; new_name: 2026-07-17_Список_Участники-слета.docx ; name_confidence 0.82 ; move_confidence 0.75 ; review true (персональные данные)"""

USER_SAMPLE = """{mode_line}СУЩЕСТВУЮЩИЕ ПАПКИ (карта системы от корня; (N) — число файлов в ветке):
{folders_block}

ФАЙЛ
Путь: {rel_path}
Имя: {name}
Размер: {size} байт | Изменён: {modified} | Содержимое: {kind}{note_line}
{source_hint}{duplicates_block}{hint_line}
СОДЕРЖИМОЕ:
{content}"""


def _render(template: str, mapping: dict) -> str:
    """Безопасная подстановка: известные ключи заменяются, прочие скобки остаются."""
    out = template
    for key, value in mapping.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _load_template(app_dir: Path, filename: str, sample: str) -> str:
    path = app_dir / filename
    if not path.exists():
        path.write_text(sample, "utf-8")
        return sample
    text = path.read_text("utf-8").strip()
    return text or sample


def _report_folder(config: dict) -> str:
    for name in config.get("top_folders", []):
        low = name.lower()
        if "отчет" in low or "отчёт" in low or "план" in low:
            return name
    return "Отчеты и планы по работе"


def build_system(config: dict, app_dir: Path | None = None) -> str:
    app_dir = Path(app_dir or config.get("app_dir") or Path(__file__).resolve().parent.parent)
    template = _load_template(app_dir, "prompt.md", SYSTEM_SAMPLE)
    routing = config.get("routing", {})
    prefix = str(config.get("mo_target_prefix", "")).strip("/")
    hide_segments = {seg for p in config.get("folders", {}).get("hide_prefixes", [])
                     for seg in str(p).replace("\\", "/").split("/") if seg and seg.strip("/") != prefix}
    top_folders = [f for f in config.get("top_folders", []) if f.casefold() not in hide_segments]
    extra_roots = [r for r in config.get("top_level_roots", []) if r and r not in top_folders]
    roots = ", ".join(list(dict.fromkeys(top_folders + extra_roots)))
    if prefix and extra_roots:
        scope = (f"Путь обязан вести в один из реальных корневых разделов: разделы регламента лежат под "
                 f"«{prefix}/», а у отдела есть и отдельные корневые папки ({', '.join(extra_roots)}). "
                 f"Бери написание дословно из «СУЩЕСТВУЮЩИЕ ПАПКИ». ")
    elif prefix:
        scope = f"Все пути начинаются с «{prefix}/» и ведут в один из перечисленных ниже разделов. "
    else:
        scope = "Бери корневой раздел дословно из «СУЩЕСТВУЮЩИЕ ПАПКИ». "
    system = _render(template, {
        "doc_types": DOC_TYPES,
        "roots": roots,
        "personal": ", ".join(routing.get("personal_subfolders", [])),
        "personal_root": routing.get("personal_root", "Личное - другие проекты"),
        "report_folder": _report_folder(config),
        "scope": scope,
    })
    try:  # накопленные уроки из ручных проверок — подсказка, но НЕ переопределяет регламент
        from .lessons import load_rules
        rules = load_rules(config).strip()
        if rules:
            system += ("\n\nУРОКИ ИЗ ПРЕДЫДУЩИХ ПРОВЕРОК (подтверждены человеком). Они уточняют "
                       "разбор только там, где НЕ противоречат правилам выше: формат имени "
                       "(ГГГГ-ММ-ДД_Тип_…) и перечень корневых разделов уроки НЕ меняют. "
                       "Если урок спорит с инструкцией — приоритет у инструкции.\n" + rules)
    except Exception:
        pass
    return system


def build_user(
    *,
    rel_path: str,
    name: str,
    size: int,
    modified: str,
    kind: str,
    note: str = "",
    folders_block: str,
    content: str,
    duplicates: list[str] | None = None,
    hint: str = "",
    mode: str = "завалы",
    app_dir: Path | None = None,
) -> str:
    app_dir = Path(app_dir or Path(__file__).resolve().parent.parent)
    template = _load_template(app_dir, "prompt_user.md", USER_SAMPLE)
    dup = [d for d in (duplicates or []) if d]
    mapping = {
        "mode_line": mode_line(mode),
        "folders_block": folders_block or "(нет подходящих веток)",
        "rel_path": rel_path,
        "name": name,
        "size": str(size),
        "modified": modified,
        "kind": kind,
        "note_line": f" | Замечание: {note}" if note else "",
        "content": (content or "").strip() or "(текст не извлечён — оцени review=true)",
        "source_hint": source_hint(rel_path),
        "duplicates_block": ("Похожие файлы рядом:\n" + "\n".join(f"  - {d}" for d in dup[:6]) + "\n") if dup else "",
        "hint_line": (f"Подсказка оператора: {hint}\n") if hint else "",
    }
    text = _render(template, mapping)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_section_user(
    *,
    rel_path: str,
    name: str,
    kind: str,
    note: str = "",
    sections_block: str,
    content: str,
    hint: str = "",
    mode: str = "завалы",
    app_dir: Path | None = None,
) -> str:
    """Запрос прохода 1: выбрать раздел (скелет карты + короткий срез содержимого)."""
    app_dir = Path(app_dir or Path(__file__).resolve().parent.parent)
    template = _load_template(app_dir, "prompt_section.md", SECTION_USER_SAMPLE)
    mapping = {
        "mode_line": mode_line(mode),
        "sections_block": sections_block or "(нет разделов)",
        "rel_path": rel_path,
        "name": name,
        "kind": kind,
        "note_line": f" | Замечание: {note}" if note else "",
        "content": (content or "").strip() or "(текст не извлечён — оцени по имени и пути)",
        "hint_line": (f"Подсказка оператора: {hint}\n") if hint else "",
    }
    text = _render(template, mapping)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def mode_line(mode: str) -> str:
    """Что сейчас делает модель — от этого зависит, можно ли менять папку."""
    key = (mode or "").strip().lower()
    if key.startswith("система") or key.startswith("сначала") or "потом" in key:
        return ("РЕЖИМ: сначала разбор постоянной системы папок, зона «Разобрать» пойдёт после. "
                "Ориентируйтесь на текущее место файла: если он уже в постоянной папке — сверяем имя и "
                "тип и меняем папку только при явной ошибке; если он из «Разобрать» — ищем постоянное место.\n")
    if key.startswith("ревиз") or key.startswith("структ"):
        return ("РЕЖИМ: ревизия существующей структуры. Файл уже лежит в постоянной папке — "
                "проверь, что имя и тип соответствуют регламенту и содержимому. Менять папку "
                "только если место явно ошибочно (тогда review=true и объясни почему).\n")
    if key.startswith("проверк") or key.startswith("систем"):
        return ("РЕЖИМ: проверка всей системы. Ищи ошибки раскладки: файл не в том разделе, "
                "дубли, материалы не отдела. Если место верное — предложи оставить как есть.\n")
    return ("РЕЖИМ: разбор завалов. Файл лежит во временной папке «Разобрать» — найди ему "
            "постоянное место в структуре ниже и имя по регламенту.\n")


def source_hint(rel_path: str) -> str:
    """Старое местоположение как подсказка: имена папок-предков часто называют событие или автора."""
    parts = [p for p in (rel_path or "").replace("\\", "/").split("/")[:-1] if p]
    if not parts:
        return ""
    junk = re.compile(r"разобр|загрузк|telegram|download|рабочий стол|^0\d_", re.I)
    useful = [p for p in parts if not junk.search(p) and p != "Документы"]
    if not useful:
        return "Текущая папка — временная «Разобрать», ориентироваться только на содержимое.\n"
    return "Текущее место (только подсказка, не доказательство): " + " / ".join(useful[-3:]) + "\n"


def parse_answer(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("пустой ответ модели")
    candidates = [raw]
    block = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if block:
        candidates.append(block.group(1).strip())
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise ValueError(f"ответ не является JSON: {raw[:160]}")


def templates_info(app_dir: Path) -> list[tuple[str, str, Path]]:
    app_dir = Path(app_dir)
    return [
        ("Системная инструкция", "prompt.md", app_dir / "prompt.md"),
        ("Шаблон запроса про файл", "prompt_user.md", app_dir / "prompt_user.md"),
        ("Шаблон выбора раздела (проход 1)", "prompt_section.md", app_dir / "prompt_section.md"),
        ("Навигатор «папка из Разобрать → раздел»", "prompt_folder.md", app_dir / "prompt_folder.md"),
    ]


def reset_template(app_dir: Path, filename: str) -> Path:
    """Вернуть заводской образец, сохранив вашу редакцию рядом с меткой времени."""
    from datetime import datetime

    samples = {"prompt.md": SYSTEM_SAMPLE, "prompt_user.md": USER_SAMPLE,
               "prompt_section.md": SECTION_USER_SAMPLE}
    if filename == "prompt_folder.md":
        from .folder_router import FOLDER_USER_SAMPLE
        samples[filename] = FOLDER_USER_SAMPLE
    path = Path(app_dir) / filename
    if path.exists():
        backup = path.with_name(f"{path.stem}.bak-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        try:
            backup.write_text(path.read_text("utf-8"), "utf-8")
        except OSError:
            pass
    path.write_text(samples.get(filename, USER_SAMPLE), "utf-8")
    return path

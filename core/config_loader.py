"""Загрузка, проверка и сохранение config.json.

Пути разрешаются относительно папки программы, поэтому комплект можно перенести
на другой диск и поправить только корень. Значения по умолчанию подобраны так,
чтобы промпт не мог случайно превысить num_ctx.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

CONFIG_NAME = "config.json"
MAP_NAME = "migration_map.csv"
LOG_NAME = "operations_log.csv"
SUGGESTIONS_NAME = "suggestions.json"
SNAPSHOT_NAME = "snapshot.json"

DEFAULTS: dict = {
    "mo_root": "C:\\Archive\\MyOrganization",
    "scan_root": "C:\\Archive\\MyOrganization",
    # Варианты корня отдела: на разных компьютерах тот же Google Drive монтируется
    # под разными буквами. При auto_pick_root=True программа берёт первый путь из
    # списка [mo_root] + root_candidates, который реально существует.
    "root_candidates": [],
    "auto_pick_root": True,
    "output_dir": "_output",
    "mo_target_prefix": "Документы",
    "ui_lang": "ru",
    "llm": {
        "enabled": True,
        "model_text": "gemma3:12b",
        "model_vision": "gemma3:12b",
        "base_url": "http://127.0.0.1:11434",
        "num_ctx": 16384,
        "num_predict": 800,
        "temperature": 0.1,
        "top_p": 0.9,
        "keep_alive": "30m",
        "timeout_seconds": 900,
        "content_chars": 6000,
        "max_pdf_pages": 12,
        "cache_policy": "nonce",
        "structured_output": True,
        "enable_thinking": False,
        "retries": 2,
        "vision": True,
        "vision_max_side": 1024,
        "vision_num_ctx": 16384,
        "default_limit": 20,
        "default_prefix": "Документы/00_Разобрать",
        "embed_model": "",
    },
    "folders": {
        "depth": 5,
        "max_paths": 900,
        "budget_chars": 22000,
        "budget_min_chars": 6000,
        "search_hits": 40,
        "show_file_counts": True,
        "hide_garbled": True,
        "hide_prefixes": ["Документы/00_Разобрать", "Документы/02_Разобрать"],
        "show_hidden_in_tree": True,
        # двухпроходный разбор: проход 1 выбирает раздел по скелету карты,
        # проход 2 ищет точное место в ПОЛНОМ поддереве выбранного раздела.
        # Так видны все глубокие ветки, какие бы они ни были — ценой ~2× запросов.
        "hierarchical": True,
        "hier_skeleton_depth": 2,
        "hier_sample_chars": 1600,
        "hier_min_confidence": 0.55,
        "hier_subtree_depth": 9,
        "hier_zoom_passes": 2,
    },
    "thresholds": {"min_confidence": 0.9, "min_name_confidence": 0.9, "min_move_confidence": 0.9,
                   "new_folder_max_confidence": 0.8},
    "queue": {"include_media": False, "order": "shallow", "mode": "завалы", "rules_first": True},
    "quarantine_dir": "99_Корзина на проверку",
    # Папки внутри «Мероприятий», которые событиями не являются: модель не должна
    # использовать их как папку проведения.
    "non_event_folders": ["Отчеты и планы по работе", "Босовец", "Ткаченко", "Отдел спорта"],
    # Двойники одного события: слева — как написано в карте, справа — канон.
    # Программа подставляет канон и предупреждает; список можно пополнять вручную.
    "folder_aliases": {
        "Документы/Мероприятия/Молодёжный образовательный форум  Жизнь вечная":
            "Документы/Мероприятия/Молодёжный образовательный форум “Жизнь вечная”",
    },
    "plugins": {"ocr": False, "doc_convert": False, "media_info": False, "name_repair": False,
                "broken_check": True, "quarantine": True, "pdf_text": True, "xlsx_read": True,
                "tesseract_cmd": ""},
    "top_folders": [
        "00_Разобрать", "Мероприятия", "Шаблоны", "Договора и программы сотрудничества",
        "Отчеты и планы по работе", "Архив (не актуальное)", "Входящие", "Исходящие",
        "Грамоты - Благодарности", "Прошения - Рапорты", "Информационное - Циркулярное",
        "Дизайн", "Поздравления", "СМИ", "Приглашения", "Документы общие",
        "Вышестоящие решения", "Методические материалы", "90_Завершённые_проекты_и_форматы",
    ],
    "routing": {
        "personal_root": "Личное - другие проекты",
        "cathedral_root": "По собору Петра и Павла",
        "personal_subfolders": ["Резьба", "Храм 1", "Храм 2", "Нужно уточнить"],
        "external": {},
    },
    "archive_extensions": [".zip", ".rar", ".7z", ".tar", ".gz"],
    "excluded_names": ["Thumbs.db", "desktop.ini", ".ds_store"],
    "excluded_prefixes": ["~$", "._", "_output", "_mo_sorter"],
    "excluded_dirs": ["__pycache__", ".git", "node_modules", "$RECYCLE.BIN"],
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _resolve(value: str, base: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path)


CHARS_PER_TOKEN = 2.6          # устойчивая оценка для русского текста
ANSWER_RESERVE_TOKENS = 900    # ответ модели + обвязка чата


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def slice_budget(config: dict, system_chars: int = 0, content_chars: int | None = None) -> int:
    """Сколько символов карты папок безопасно влезает при текущем num_ctx.

    Из контекста вычитаем системную инструкцию (она в каждом запросе), текст
    файла, запас на ответ и шаблон чата. Остаток — бюджет среза папок.
    Честный маленький остаток лучше floor-заведомого перероста.
    """
    llm = config.get("llm", {})
    folders = config.get("folders", {})
    num_ctx = _as_int(llm.get("num_ctx", 16384), 16384)
    content = _as_int(llm.get("content_chars", 9000) if content_chars is None else content_chars, 9000)
    used = (int(system_chars) + content) / CHARS_PER_TOKEN + ANSWER_RESERVE_TOKENS
    room = int(max(0, num_ctx - used) * CHARS_PER_TOKEN)
    configured = _as_int(folders.get("budget_chars", 22000), 22000)
    return int(min(configured, max(800, room)))


def load_config(app_dir: Path | None = None, write_if_missing: bool = True,
                config_name: str = CONFIG_NAME) -> dict:
    if app_dir is None:
        import sys
        app_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                   else Path(__file__).resolve().parent.parent)
    app_dir = Path(app_dir).resolve()
    config_path = app_dir / (config_name or CONFIG_NAME)
    if not config_path.exists() and write_if_missing:
        config_path.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), "utf-8")
    try:
        raw = json.loads(config_path.read_text("utf-8")) if config_path.exists() else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"config.json повреждён ({exc}). Почините его или переименуйте — "
            "программа создаст новый по настройкам по умолчанию.") from exc
    if not isinstance(raw, dict):
        raw = {}
    config = _merge(DEFAULTS, raw)

    problems: list[str] = []
    base_url = str(config["llm"]["base_url"]).strip()
    if not base_url.startswith("http"):
        base_url = "http://127.0.0.1:11434"
        problems.append("base_url был некорректным — подставлен локальный адрес Ollama")
    config["llm"]["base_url"] = base_url.rstrip("/")

    num_ctx = _as_int(config["llm"]["num_ctx"], DEFAULTS["llm"]["num_ctx"])
    if num_ctx != config["llm"]["num_ctx"]:
        problems.append(f"llm.num_ctx={config['llm']['num_ctx']!r} не число — взято {num_ctx}")
        config["llm"]["num_ctx"] = num_ctx
    if num_ctx < 4096:
        problems.append(f"num_ctx={num_ctx} мал для промпта с правилами; нужно не меньше 4096")
    prompt_file = app_dir / "prompt.md"
    system_chars = len(prompt_file.read_text("utf-8")) if prompt_file.exists() else 0
    config["system_prompt_chars"] = system_chars
    room = slice_budget(config, system_chars)
    budget = _as_int(config["folders"]["budget_chars"], DEFAULTS["folders"]["budget_chars"])
    if budget != config["folders"]["budget_chars"]:
        problems.append(f"folders.budget_chars={config['folders']['budget_chars']!r} не число — "
                        f"взято {budget}")
        config["folders"]["budget_chars"] = budget
    if budget > room:
        problems.append(
            f"карта папок {budget} симв. не влезает в num_ctx={num_ctx}: инструкция занимает "
            f"~{int(system_chars / CHARS_PER_TOKEN)} ток., текст файла ~"
            f"{_as_int(config['llm'].get('content_chars'), 9000) // int(CHARS_PER_TOKEN)} ток.; "
            f"срез будет ужат до {room} симв. "
            f"или увеличьте num_ctx"
        )
        config["folders"]["budget_chars_effective"] = room
    else:
        config["folders"]["budget_chars_effective"] = budget

    config["app_dir"] = app_dir

    # --- авто-выбор корня: на этом компьютере тот же диск может быть под другой буквой ---
    def _norm(p: str) -> str:
        return str(_resolve(p, app_dir)).rstrip("\\/").casefold()

    candidates = [str(config["mo_root"])] + [str(c) for c in config.get("root_candidates", []) if str(c).strip()]
    # убираем дубликаты, сохраняя порядок
    ordered: list[str] = []
    for cand in candidates:
        if _norm(cand) not in {_norm(x) for x in ordered}:
            ordered.append(cand)
    chosen_root = None
    if bool(config.get("auto_pick_root", True)):
        for cand in ordered:
            if _resolve(cand, app_dir).is_dir():
                chosen_root = cand
                break
    else:
        chosen_root = str(config["mo_root"])

    old_root = _norm(str(config["mo_root"]))
    def _remap(value: str) -> str:
        """Если value лежит под старым корнем, переносим его под выбранный корень."""
        if chosen_root is None:
            return value
        rs = str(_resolve(value, app_dir)).rstrip("\\/")
        rsc = rs.casefold()
        if rsc == old_root:
            return chosen_root
        if rsc.startswith(old_root + "\\"):
            return chosen_root.rstrip("\\/") + rs[len(old_root):]
        return rs

    if chosen_root is not None and _norm(chosen_root) != old_root:
        config["mo_root"] = _remap(str(config["mo_root"]))
        config["scan_root"] = _remap(str(config["scan_root"]))
        config["output_dir"] = _remap(str(config["output_dir"]))

    config["mo_root"] = _resolve(config["mo_root"], app_dir)
    config["scan_root"] = _resolve(config["scan_root"], app_dir)
    config["output_path"] = _resolve(config["output_dir"], app_dir)
    config["config_path"] = config_path

    routing = config["routing"]
    routing["external"] = {k: _resolve(v, app_dir) for k, v in dict(routing.get("external") or {}).items()}
    names = {str(name).casefold() for name in config["excluded_names"]}
    config["excluded_names"] = names
    config["excluded_dirs"] = {str(folder).casefold() for folder in config["excluded_dirs"]}
    config["excluded_prefixes"] = tuple(config["excluded_prefixes"])
    config["archive_extensions"] = tuple(config["archive_extensions"])
    if not config["top_folders"]:
        problems.append("не задан список top_folders — модели будет не из чего выбирать раздел")
    if not config["scan_root"].is_dir():
        problems.append(f"папка сканирования не найдена: {config['scan_root']}")
    config["problems"] = problems
    return config


def save_config(config: dict) -> Path:
    """Записать обратно настраиваемые поля, не трогая вычисленные пути."""
    skip = {"app_dir", "output_path", "config_path", "problems", "system_prompt_chars"}
    payload = {key: value for key, value in config.items() if key not in skip}
    if isinstance(payload.get("folders"), dict):
        payload["folders"] = {k: v for k, v in payload["folders"].items() if k != "budget_chars_effective"}
    for key in ("mo_root", "scan_root", "output_dir"):
        if key in config:
            payload[key] = str(config[key])
    payload["excluded_names"] = sorted(config.get("excluded_names", []))
    payload["excluded_dirs"] = sorted(config.get("excluded_dirs", []))
    routing = payload.get("routing", {})
    if isinstance(routing.get("external"), dict):
        routing["external"] = {key: str(value) for key, value in routing["external"].items()}
    path = Path(config.get("config_path", Path.cwd() / CONFIG_NAME))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    return path


def describe_paths(config: dict) -> list[tuple[str, str]]:
    return [
        ("Корень отдела", str(config["mo_root"])),
        ("Что сканируем", str(config["scan_root"])),
        ("Служебная папка", str(config["output_path"])),
        ("Файл настроек", str(config["config_path"])),
    ]

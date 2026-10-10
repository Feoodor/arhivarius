"""Аудит структуры диска и черновик целевого дерева.

Задача — не «перемолоть» файлы, а навести порядок в самом пространстве: показать,
где дерево разошлось с регламентом, и предложить компактный каркас, в котором
слабой модели не придётся выдумывать папки. Отчёт только читает; создание папок
отдельное и запускается человеком по списку.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .folders import FolderIndex, norm

DATE_IN_NAME = re.compile(r"(?:(?:19|20)\d{2})[-.](\d{2})?(?:[-.](\d{2}))?|(?<!\d)(?:19|20)\d{2}(?!\d)")
YEAR_ONLY = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _top(path: str) -> str:
    return path.split("/")[0] if path else ""


def analyze(config: dict, index: FolderIndex) -> dict:
    """Собрать отклонения текущего дерева от регламента."""
    allowed = {norm(name): name for name in config.get("top_folders", [])}
    routing = config.get("routing", {})
    for extra in [routing.get("personal_root", ""), routing.get("cathedral_root", ""),
                  *config.get("top_level_roots", [])]:
        if extra:
            allowed.setdefault(norm(extra), extra)
    prefix = str(config.get("mo_target_prefix", "")).strip("/")
    intake = [str(p).strip("/") for p in config.get("folders", {}).get("hide_prefixes", [])]

    findings: dict = {
        "корень": str(config["scan_root"]),
        "когда": datetime.now().isoformat(timespec="seconds"),
        "лишние_разделы": [],
        "пустые_папки": [],
        "слишком_глубоко": [],
        "дубли_названий": [],
        "файлы_в_корне": [],
        "зоны_разбора": [],
        "к_подъёму": [],
        "счётчик": {},
    }

    for child in index.root.children.values():
        if child.depth == 1 and norm(child.name) not in allowed and child.name != prefix:
            findings["лишние_разделы"].append({"путь": child.path, "файлов": child.total})

    for path, node in index.by_path.items():
        if not path:
            continue
        if node.total == 0:
            findings["пустые_папки"].append(path)
        if path.count("/") >= 5:
            findings["слишком_глубоко"].append({"путь": path, "файлов": node.total})

    by_name: dict[str, list[str]] = defaultdict(list)
    for path, node in index.by_path.items():
        if path and node.depth >= 2:
            by_name[norm(node.name)].append(path)
    for key, paths in by_name.items():
        if len(paths) > 1 and sum(index.by_path[p].total for p in paths) >= 5:
            findings["дубли_названий"].append({"имя": key, "варианты": sorted(paths)[:6], "файлов": sum(
                index.by_path[p].total for p in paths)})

    for zone in intake:
        node = index.by_path.get(zone)
        if node is None:
            continue
        findings["зоны_разбора"].append({"путь": zone, "файлов": node.total, "подпапок": len(node.children)})
        for child in node.children.values():
            if child.total >= 20:
                year = YEAR_ONLY.search(child.name)
                findings["к_подъёму"].append({
                    "путь": child.path,
                    "файлов": child.total,
                    "год": year.group(0) if year else "",
                    "предложение": _suggest_target(child, config),
                })
    findings["к_подъёму"].sort(key=lambda row: -row["файлов"])

    top_files = Counter()
    for path, node in index.by_path.items():
        if node.depth == 1:
            top_files[path] = node.total
    findings["счётчик"] = {
        "папок": index.folder_count,
        "файлов": index.root.total,
        "разделов_верхнего_уровня": len(index.root.children),
    }
    return findings


def _suggest_target(node, config: dict) -> str:
    """Куда логично перенести большую свалку из зоны разбора."""
    name = node.name
    year = YEAR_ONLY.search(name)
    tail = f"/{year.group(0)}" if year else ""
    cleaned = re.sub(r"[\s_]+", "-", name.strip())[:60]
    if re.search(r"фото|img|dsc|сним", name, re.I):
        return f"Мероприятия/Фотоархив{tail}/{cleaned}"
    if re.search(r"рапорт|прошени|вх|исх|письм", name, re.I):
        return f"Прошения - Рапорты{tail}"
    if re.search(r"отчет|отчёт|план|свод", name, re.I):
        return f"Отчеты и планы по работе{tail}"
    if re.search(r"смет|финан|расход", name, re.I):
        return f"Финансы{tail}"
    if re.search(r"слет|слёт|фестивал|игра|бал|палом|тур", name, re.I):
        return f"Мероприятия/События{tail}/{cleaned}"
    return f"Документы общие{tail}/{cleaned}"


def skeleton(config: dict) -> list[str]:
    """Целевой каркас: не больше трёх ярусов до конкретного события."""
    years = ["{Год}"]
    rows = [
        "00_Разобрать/                       вход без подпапок: сюда складываем непонятное",
        "Мероприятия/{Формат}/{Год}/{ГГГГ-ММ-ДД_Название-события}/",
        "  01_Официальные-документы/  02_Программа-и-сценарий/  03_Фото/  04_Посты-и-анонсы/",
        "  05_Финансы/  06_Участники_ограниченный-доступ/  07_Безопасность-и-логистика/",
        "Входящие/{Год}/        Исходящие/{Год}/",
        "Отчеты и планы по работе/{Год}/",
        "Прошения - Рапорты/{Год}/",
        "Грамоты - Благодарности/{Год}/",
        "Договора и программы сотрудничества/{Год}/",
        "Методические материалы/{Тема}/",
        "Шаблоны/                             TPL_Тип_Назначение.ext",
        "Дизайн/{Год}/",
        "СМИ/{Год}/",
        "Документы общие/{Тема}/",
        "Архив (не актуальное)/{Год}/",
        "90_Завершённые-проекты-и-форматы/{Название}/",
        "99_Корзина на проверку/               только ручная чистка",
    ]
    return rows


PLAN_NAME = "structure_plan.txt"
CREATED_NAME = "structure_created.csv"

# Разделы, которые можно создать как есть. Плейсхолдеры {Год}, {Формат}, {Тема}
# программа не выдумывает: их порождает содержимое файлов при разборе.
OPEN_ENDED = ("Мероприятия", "Методические материалы", "Документы общие",
              "90_Завершённые_проекты_и_форматы", "Дизайн", "СМИ")


def concrete_plan(config: dict, years: tuple[str, ...] = ()) -> list[str]:
    """Список папок, которые безопасно создать прямо сейчас."""
    plan: list[str] = []
    for name in config.get("top_folders", []):
        root = str(name).strip("/")
        if not root:
            continue
        plan.append(root)
        if root in OPEN_ENDED:
            continue
        for year in years:
            plan.append(f"{root}/{year}")
    quarantine = str(config.get("quarantine_dir", "")).strip("/")
    if quarantine:
        plan.append(quarantine)
    return plan


def write_plan(config: dict, rows: list[str]) -> Path:
    target = config["output_path"] / PLAN_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    header = ("# По одной папке на строку; путь от корня сканирования или от «%s».\n"
              "# Строки с # игнорируются. Отредактируйте файл и нажмите «Создать папки из плана».\n"
              % config.get("mo_target_prefix", ""))
    target.write_text(header + "\n".join(rows) + "\n", "utf-8")
    return target


def read_plan(config: dict) -> list[str]:
    target = config["output_path"] / PLAN_NAME
    if not target.exists():
        return []
    return [line.strip() for line in target.read_text("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def create_paths(config: dict, paths: list[str], apply: bool = False, log=None) -> dict:
    """Создать согласованные папки. Существовавшие не трогаются, удалений нет."""
    say = log or (lambda message: None)
    root: Path = config["scan_root"]
    prefix = str(config.get("mo_target_prefix", "")).strip("/")
    created: list[str] = []
    existed: list[str] = []
    refused: list[str] = []
    for raw in paths:
        clean = (raw or "").strip().replace("\\", "/").strip().strip("/")
        if not clean:
            continue
        if ".." in clean.split("/") or re.match(r"^[a-zA-Z]:", clean):
            refused.append(f"{clean} — путь вне рабочего каталога")
            continue
        if prefix and clean.casefold() != prefix.casefold() \
                and not clean.casefold().startswith(prefix.casefold() + "/"):
            clean = f"{prefix}/{clean}"
        target = root / clean
        if target.is_dir():
            existed.append(clean)
            continue
        if not apply:
            created.append(clean)
            say("будет создана: " + clean)
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            created.append(clean)
            say("создана: " + clean)
        except OSError as exc:
            refused.append(f"{clean} — {type(exc).__name__}: {exc}")
    report = config["output_path"] / CREATED_NAME
    report.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    mode = "создано" if apply else "только план"
    with report.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Дата", "Режим", "Папка", "Итог"])
        for row in created:
            writer.writerow([stamp, mode, row, "создана" if apply else "папки нет"])
        for row in existed:
            writer.writerow([stamp, mode, row, "уже есть"])
        for row in refused:
            path, _, why = row.partition(" — ")
            writer.writerow([stamp, mode, path, why or "отклонено"])
    return {"создано": len(created) if apply else 0, "в плане": len(created),
            "уже есть": len(existed), "отказов": len(refused), "отчёт": str(report)}


def render_report(config: dict, findings: dict) -> str:
    """Человекочитаемый отчёт: что не так и что предлагаем."""
    lines = [
        "# Аудит структуры диска отдела",
        f"Корень: {findings['корень']}  ·  снято {findings['когда']}",
        "",
        f"Папок: {findings['счётчик']['папок']} · файлов: {findings['счётчик']['файлов']} · "
        f"разделов верхнего уровня: {findings['счётчик']['разделов_верхнего_уровня']}",
        "",
        "## 1. Отклонения от регламента",
        "",
    ]
    extra = findings["лишние_разделы"]
    lines.append(f"- разделов верхнего уровня вне перечня: {len(extra)}")
    for row in extra[:12]:
        lines.append(f"  - {row['путь']}  ({row['файлов']} файлов)")
    lines.append(f"- пустых папок: {len(findings['пустые_папки'])} — по регламенту их быть не должно")
    for path in findings["пустые_папки"][:12]:
        lines.append(f"  - {path}")
    lines.append(f"- вложеннее 5 ярусов: {len(findings['слишком_глубоко'])} — такие ветки модель не охватывает")
    for row in findings["слишком_глубоко"][:10]:
        lines.append(f"  - {row['путь']}  ({row['файлов']})")
    lines.append(f"- одинаковых названий в разных местах: {len(findings['дубли_названий'])} — риск «двух канонов»")
    for row in findings["дубли_названий"][:12]:
        lines.append(f"  - «{row['имя']}» × {len(row['варианты'])}, всего файлов {row['файлов']}")
        for variant in row["варианты"][:3]:
            lines.append(f"      · {variant}")
    zones = ", ".join(f"{z['путь']} ({z['файлов']} файлов, подпапок {z['подпапок']})" for z in findings["зоны_разбора"])
    lines += ["", f"## 2. Временная зона разбора", "", f"  {zones or '—'}",
              "  Внутри неё порядок не наводится: её содержимое распределяется по разделам ниже."]

    lines += ["", "## 3. Крупные свалки, которые просятся в структуру", ""]
    if not findings["к_подъёму"]:
        lines.append("  таких нет — зона разбора пуста или дробная")
    for row in findings["к_подъёму"][:14]:
        lines.append(f"  - {row['путь']}  ({row['файлов']} файлов)  →  {row['предложение']}")

    lines += ["", "## 4. Целевой каркас (не больше 3 ярусов до события)", "", "```"]
    lines += skeleton(config)
    lines += ["```", "",
              "## 5. Порядок действий",
              "  1. Согласовать каркас (разделы и имена) — правки прямо в этом файле.",
              "  2. Создать пустые разделы каркаса вручную или командой `structure --create` (создаёт только согласованное).",
              "  3. Режим «🧭 Ревизия структуры»: проверить имена и места уже разобранных файлов.",
              "  4. Режим «🧹 Разбор завалов»: распределять содержимое 00_Разобрать по каркасу партиями.",
              "  5. Крупные свалки из пункта 3 поднимать целиком, а не по одному файлу.",
              ""]
    return "\n".join(lines)

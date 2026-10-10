"""Страховки: здесь программа перестаёт верить модели на слово.

Модель может ошибиться в имени, придумать год или изобрести раздел, которого
нет в регламенте. Каждое предложение проходит через этот фильтр: несогласованное
исправляется либо уходит на human-review с объясняющей меткой. Ничего не
перемолчается и не удалится молча.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from .extract import DATE_RE, ISO_RE, Extracted
from .folders import FolderIndex, is_garbled, norm, tokens

FORBIDDEN_CHARS = '<>:"/\\|?*' + "\t"
BAD_WORDS = ("финал", "новый", "последний", "чистовик", "копия", "copy", "final", "new", "isco")
DATE_HEAD = re.compile(r"^(19|20)\d{2}(-\d{2}){0,2}")
YEAR_IN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
INTAKE_RE = re.compile(r"разобр", re.IGNORECASE)
MAX_NAME = 120


def conf_value(value) -> float | None:
    """Число уверенности из ответа модели: 0..1 либо None (нет/не число/вне диапазона)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0 or v > 1:
        return None
    return v


def resolve_confidences(answer: dict) -> tuple[float, float]:
    """Вернуть (уверенность_в_имени, уверенность_в_переносе).

    Модель просят заполнять name_confidence и move_confidence, а общее поле
    confidence она вправе оставить нулём. Если хотя бы одно из двух чисел
    заполнено — верим им, недостающее считаем нулём (это уводит строку на
    проверку, а не выдумывает уверенность). Если оба пусты/нули, а confidence
    заполнен (старый/одним-полем ответ) — зеркалим confidence в обе.
    """
    name = conf_value(answer.get("name_confidence"))
    move = conf_value(answer.get("move_confidence"))
    overall = conf_value(answer.get("confidence"))
    if not name and not move and overall:
        return overall, overall
    return (name or 0.0), (move or 0.0)


@dataclass
class Verdict:
    record: dict = field(default_factory=dict)
    review: bool = False
    flags: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    def flag(self, text: str) -> None:
        if text and text not in self.flags:
            self.flags.append(text)
            self.review = True

    def fix(self, text: str) -> None:
        if text and text not in self.fixed:
            self.fixed.append(text)


def clean_segment(value: str) -> str:
    value = value.replace("№", "").strip()
    value = re.sub(r"[<>:\"\\|?*`]", "", value)
    value = re.sub(r"\s*\.\s*$", "", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" .")   # длинные имена папок НЕ обрезаем — это настоящие названия


def clean_file_name(value: str, extension: str) -> str:
    """Имя по регламенту: без пробелов, без мусора, с исходным расширением."""
    value = (value or "").strip().replace("\\", "/").rpartition("/")[2]
    ext = (extension or "").lower()
    stem = value
    if ext and value.lower().endswith(ext):
        stem = value[: -len(ext)]
    stem = stem.replace("№", "")
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f`'\"“”«»()]", "", stem)
    stem = re.sub(r"[\s_]+", "_", stem)
    kept = []
    for block in stem.split("_"):
        core = re.sub(r"\d+$", "", block).casefold()
        if core and core in BAD_WORDS:
            continue
        kept.append(block)
    stem = "_".join(b for b in kept if b)
    stem = re.sub(r"[._]{2,}", "_", stem)
    # «2017-Отчёт» и «2025-06-20-Тема» → блоки разделяются подчёркиванием,
    # но внутренние дефисы даты (2025-06-20) остаются как есть.
    stem = re.sub(r"^((?:19|20)\d{2}(?:-\d{2}){2}|(?:19|20)\d{2})-(?=[^\d])", r"\1_", stem)
    stem = stem.strip("_.")
    if len(stem) > MAX_NAME:
        stem = stem[:MAX_NAME].rstrip("_-. ")
    ext = (extension or "").lower()
    name = f"{stem}{ext}" if stem else f"Безымянный{ext or '.bin'}"
    return re.sub(r"^_+", "", name)


def normalize_target(target: str, config: dict, index: FolderIndex, verdict: Verdict) -> str:
    """Починить путь: слэши, лишний корень, опечатки в названиях разделов.

    Сначала якорим ПЕРВЫЙ сегмент к реальному корню дерева или к разделу
    регламента (в т.ч. нечётко), и только потом решаем, нужен ли префикс
    «Документы/». Иначе реальные корни («По собору…», «Направления…») ошибочно
    уезжали внутрь Документов и плодили фантомные ветки.
    """
    raw = (target or "").replace("\\", "/").strip().strip("/")
    raw = re.sub(r"/{2,}", "/", raw)
    if not raw:
        verdict.flag("модель не указала целевую папку")
        return ""

    prefix = str(config.get("mo_target_prefix", "")).strip("/")
    parts = [clean_segment(p) for p in raw.split("/") if p.strip()]
    parts = [p for p in parts if p and not is_garbled(p)]
    if not parts:
        verdict.flag("в пути нет корневого раздела")
        return ""

    fold_prefix = fold(prefix)
    real_roots = {fold(r): r for r in sorted(index.top_levels) if r}
    top_children = {fold(t): t for t in config.get("top_folders", []) if str(t).strip()}
    try:
        mo_name = fold(Path(str(config.get("mo_root", ""))).name)
    except Exception:
        mo_name = ""
    junk_heads = {j for j in (mo_name, "общие диски", "общие_диски", "мо") if j}
    known_heads = set(real_roots) | set(top_children) | {fold_prefix}

    for _step in range(4):
        f0 = fold(parts[0])
        if len(parts) > 1 and f0 in junk_heads and fold(parts[1]) in known_heads:
            verdict.fix(f"убран лишний префикс пути «{parts[0][:40]}»")
            parts = parts[1:]
            continue
        if (f0 == fold_prefix and len(parts) > 1
                and fold(parts[1]) in real_roots and fold(parts[1]) != fold_prefix):
            verdict.fix(f"«{prefix}» убран: «{parts[1][:40]}» — самостоятельный корневой раздел")
            parts = parts[1:]
            continue
        if f0 == fold_prefix:
            parts[0] = prefix
            break
        if f0 in real_roots:
            parts[0] = real_roots[f0]
            break
        if f0 in top_children:
            parts[0] = top_children[f0]
            break
        root_hit, root_score = _match_root_scored(
            parts[0], [r for k, r in real_roots.items() if k != fold_prefix])
        child_hit, child_score = _match_root_scored(parts[0], list(top_children.values()))
        if child_hit and child_score >= 60 and child_score >= root_score:
            verdict.fix(f"раздел «{parts[0][:40]}» приведён к «{child_hit}»")
            parts[0] = child_hit
            break
        if root_hit and root_score >= 60:
            verdict.fix(f"«{parts[0][:40]}» распознан как корневой раздел «{root_hit}» — без префикса")
            parts[0] = root_hit
            break
        if len(parts) > 1 and fold(parts[1]) in known_heads:
            verdict.fix(f"убран лишний первый сегмент «{parts[0][:40]}»")
            parts = parts[1:]
            continue
        break

    anchored = fold(parts[0]) in real_roots or (prefix and fold(parts[0]) == fold_prefix)
    if prefix and not anchored:
        parts = [prefix, *parts]
        verdict.fix(f"добавлен префикс раздела «{prefix}»")
    path = "/".join(parts)
    head = 1 if (prefix and parts[0].casefold() == prefix.casefold()) else 0
    top = parts[head] if len(parts) > head else ""
    if not top:
        verdict.flag("в пути нет корневого раздела")
        return path

    allowed = [f for f in config.get("top_folders", [])] + [
        str(config.get("routing", {}).get("personal_root", "")),
        str(config.get("routing", {}).get("cathedral_root", "")),
        *sorted(real_roots.values()),
    ]
    allowed = [a for a in allowed if a]
    known = {norm(a) for a in allowed}
    if norm(top) not in known:
        replacement = _match_root(top, allowed)
        if replacement:
            parts[parts.index(top)] = replacement
            path = "/".join(parts)
            verdict.fix(f"корневой раздел «{top}» приведён к «{replacement}»")
        else:
            verdict.flag(f"раздел «{top}» не из перечня регламента")
    return path


def _match_root_scored(candidate: str, allowed: list[str]) -> tuple[str, int]:
    cand = norm(candidate)
    cand_tokens = set(tokens(candidate))
    best, best_score = "", 0
    for root in allowed:
        have = norm(root)
        score = 0
        if cand == have:
            score = 100
        elif cand and (cand in have or have in cand):
            score = 60 + min(len(cand), len(have))
        else:
            overlap = len(cand_tokens & set(tokens(root)))
            if overlap:
                score = 40 * overlap + max(
                    (len(a) for a in cand_tokens for b in set(tokens(root)) if a[:4] == b[:4]), default=0
                )
        if score > best_score:
            best, best_score = root, score
    return best, best_score


def _match_root(candidate: str, allowed: list[str]) -> str:
    best, best_score = _match_root_scored(candidate, allowed)
    return best if best_score >= 40 else ""


def check_name_date(name: str, extracted: Extracted, old_name: str, modified: str, verdict: Verdict) -> None:
    """Год/дата в имени обязаны быть чем-то подтверждены — иначе это выдумка."""
    found = YEAR_IN.findall(name)
    if not found:
        return
    evidence = set(extracted.years) | set(YEAR_IN.findall(old_name))
    evidence_dates = set(extracted.dates)
    # EXIF съёмки — такое же доказательство, как текст файла (формат «ГГГГ:ММ:ДД чч:мм»)
    exif_stamp = str(extracted.meta.get("DateTimeOriginal") or extracted.meta.get("DateTime") or "")
    m_exif = re.match(r"^(\d{4})[:\-.](\d{2})[:\-.](\d{2})", exif_stamp)
    if m_exif:
        evidence_dates.add(f"{m_exif.group(1)}-{m_exif.group(2)}-{m_exif.group(3)}")
        evidence.add(m_exif.group(1))
    for day, month, year in DATE_RE.findall(old_name):
        if 1 <= int(month) <= 12:
            evidence_dates.add(f"{year}-{int(month):02d}-{int(day):02d}")
    for year, month, day in ISO_RE.findall(old_name):
        evidence_dates.add(f"{year}-{month}-{day}")
    stamp = (modified or "").replace("/", "-")
    if len(stamp) >= 10:
        evidence_dates.add(stamp[:10])
        evidence.add(stamp[:4])
    for iso in re.findall(r"(?:19|20)\d{2}-\d{2}-\d{2}", name):
        if iso not in evidence_dates:
            verdict.flag(f"дата {iso} не подтверждена текстом файла")
    for year in found:
        if year in evidence:
            continue
        try:
            if year == datetime.fromisoformat(modified).strftime("%Y"):
                continue
        except Exception:
            pass
        verdict.flag(f"год {year} не подтверждён содержимым")


def path_conflicts(target: str, name: str, root: Path) -> str:
    """Есть ли уже такой файл — чтобы не затирать чужое."""
    candidate = root / PurePosixPath(f"{target}/{name}")
    return str(candidate) if candidate.exists() else ""


KNOWN_DOC_EXTS = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".pdf", ".txt", ".csv", ".md", ".jpg", ".jpeg",
    ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".zip", ".rar", ".7z", ".msg", ".rtf",
}


def strip_file_from_target(target: str, new_name: str, old_name: str, verdict: Verdict) -> str:
    """Убрать имя файла из пути папки: модель часто возвращает путь вместе с файлом."""
    parts = [p for p in (target or "").split("/") if p]
    if not parts:
        return target
    last = parts[-1]
    lowered = last.casefold()
    suspects = {new_name.casefold(), old_name.casefold(), Path(new_name).stem.casefold(), Path(old_name).stem.casefold()}
    looks_like_file = lowered in suspects or any(lowered.endswith(ext) for ext in KNOWN_DOC_EXTS)
    if not looks_like_file:
        return target
    trimmed = "/".join(parts[:-1])
    verdict.fix("имя файла убрано из целевого пути (там должна быть только папка)")
    return trimmed


def canon_type_block(name: str) -> str:
    """Тип документа — с большой буквы и с «ё», как в перечне регламента."""
    from .prompts import DOC_TYPES

    forms = {t.strip().casefold().replace("ё", "е"): t.strip() for t in DOC_TYPES.split(",")}
    blocks = name.split("_")
    for i, block in enumerate(blocks[:3]):
        head = block.split("-")[0]
        want = forms.get(head.casefold().replace("ё", "е"))
        if want and head != want:
            blocks[i] = want + block[len(head):]
    return "_".join(blocks)


def fold(value: str) -> str:
    return (value or "").casefold().replace("ё", "е")


def resolve_path(target: str, index: FolderIndex, verdict: Verdict) -> str:
    """Сопоставить путь с реальным деревом, прощая ё/е, регистр, лишние пробелы
    и обрезанные/неточные имена сегментов (единственное совпадение по префиксу)."""
    if not target or target in index.by_path:
        return target
    folded = {fold(path): path for path in index.by_path if path}
    hit = folded.get(fold(target))
    if hit:
        verdict.fix(f"папка найдена с учётом написания (ё/е, регистр): «{hit}»")
        return hit
    resolved: list[str] = []
    for part in [p for p in target.split("/") if p]:
        candidate = "/".join(resolved + [part])
        if candidate in index.by_path:
            resolved.append(part)
            continue
        near = folded.get(fold(candidate))
        if near:
            resolved = near.split("/")
            continue
        parent = "/".join(resolved)
        parent_node = index.by_path.get(parent) if resolved else index.root
        fp = fold(part)
        cands = []
        if parent_node is not None and fp:
            for cname in parent_node.children:
                fc = fold(cname)
                if fc == fp or fc.startswith(fp) or (len(fp) >= 6 and fp.startswith(fc)):
                    cands.append(cname)
        if len(cands) == 1 and cands[0] != part:
            resolved.append(cands[0])
            verdict.fix(f"папка «{part[:40]}…» уточнена по дереву до «{cands[0][:60]}»")
            continue
        resolved.append(part)
    result = "/".join(resolved)
    if result and result != target and result in index.by_path:
        verdict.fix("путь уточнён по написанию папок системы")
        return result
    return target


def canonicalize_target(target: str, config: dict, verdict: Verdict) -> str:
    """Слить двойники: если путь совпадает с папкой-дублем, берём каноническую запись."""
    aliases = config.get("folder_aliases") or {}
    if not target or not aliases:
        return target
    # norm(), а не fold(): двойники различаются кавычками и лишними пробелами
    folded = {norm(src): (src, dst) for src, dst in aliases.items() if norm(src)}
    parts = target.split("/")
    for cut in range(len(parts), 0, -1):
        head = "/".join(parts[:cut])
        hit = folded.get(norm(head))
        if not hit:
            continue
        src, dst = hit
        rest = parts[cut:]
        new = "/".join([dst.strip("/"), *rest]) if rest else dst.strip("/")
        verdict.fix(f"двойник «{src.rpartition('/')[2]}» сведён к «{dst.rpartition('/')[2]}» — так в карте больше файлов")
        return new
    return target


def check_non_event_folder(target: str, config: dict, verdict: Verdict) -> None:
    """В «Мероприятиях» лежат и служебные папки — как папку события использовать нельзя."""
    banned = {fold(name) for name in config.get("non_event_folders", []) if name}
    if not target or not banned:
        return
    parts = [p for p in target.split("/") if p]
    for position, part in enumerate(parts):
        if fold(part) != fold("Мероприятия") or position + 1 >= len(parts):
            continue
        child = parts[position + 1]
        if fold(child) in banned:
            verdict.flag(
                f"«{child}» внутри «Мероприятий» — не событие (служебная папка): "
                "материал события сюда класть нельзя, перенесите или заведите папку события"
            )
            return


def normalize_shortcut(raw: str, config: dict, index: FolderIndex, verdict: Verdict,
                       direction: str, target: str, years: list[str]) -> str:
    """Папка ярлыка для письма о событии: Входящие/ГГГГ или Исходящие/ГГГГ."""
    prefix = str(config.get("mo_target_prefix", "")).strip("/")
    roots = {"вх": "Входящие", "исх": "Исходящие"}
    want_root = roots.get(direction, "")
    value = (raw or "").replace("\\", "/").strip().strip("/")
    if value:
        value = re.sub(r"/{2,}", "/", value)
        if prefix and value.casefold().startswith(prefix.casefold() + "/"):
            value = value[len(prefix) + 1:]
        head = value.split("/")[0]
        if fold(head) not in {fold("Входящие"), fold("Исходящие")}:
            verdict.flag(f"ярлык «{value}» ведёт не во Входящие/Исходящие — поле очищено")
            value = ""
        elif want_root and fold(head) != fold(want_root):
            verdict.fix(f"направление «{direction}»: ярлык переставлен в «{want_root}»")
            value = "/".join([want_root, *value.split("/")[1:]])
    if not value and want_root and target and fold("Мероприятия") in [fold(p) for p in target.split("/")]:
        year = next((y for y in years if re.fullmatch(r"(19|20)\d{2}", y or "")), "")
        if year:
            value = f"{want_root}/{year}"
            verdict.fix(f"письмо о событии без ярлыка — добавлен «{value}» (год по дате документа)")
    if not value:
        return ""
    full = f"{prefix}/{value}" if prefix and not value.casefold().startswith(prefix.casefold() + "/") else value
    return resolve_path(full, index, verdict)


ISO_FIELD = re.compile(r"^(?P<y>(?:19|20)\d{2})(?:[-./](?P<m>\d{1,2}))?(?:[-./](?P<d>\d{1,2}))?$")
RU_FIELD = re.compile(r"^(?P<d>\d{1,2})[.-](?P<m>\d{1,2})[.-](?P<y>(?:19|20)\d{2})$")


def iso_field(value) -> str:
    """Дата из ответа модели → ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ. Пусто, если не дата."""
    text = str(value or "").strip().replace(",", ".").rstrip(".")
    if not text:
        return ""
    ru = RU_FIELD.match(text)
    if ru:
        day, month, year = int(ru.group("d")), int(ru.group("m")), ru.group("y")
        return f"{year}-{month:02d}-{day:02d}" if 1 <= month <= 12 and 1 <= day <= 31 else ""
    match = ISO_FIELD.match(text)
    if not match:
        return ""
    year = match.group("y")
    month = int(match.group("m") or 0)
    day = int(match.group("d") or 0)
    if month and not 1 <= month <= 12:
        return ""
    if day and not 1 <= day <= 31:
        return ""
    if month and day:
        return f"{year}-{month:02d}-{day:02d}"
    if month:
        return f"{year}-{month:02d}"
    return year


def build_record(
    item: dict,
    extracted: Extracted,
    answer: dict,
    index: FolderIndex,
    config: dict,
    root: Path,
    duplicates: list[str],
    usage: dict | None = None,
) -> Verdict:
    verdict = Verdict()
    old_path = str(item.get("path", "")).replace("\\", "/")
    old_name = Path(old_path).name
    extension = Path(old_path).suffix.lower()

    thresholds = config.get("thresholds", {})
    min_name = float(thresholds.get("min_name_confidence", thresholds.get("min_confidence", 0.9)))
    min_move = float(thresholds.get("min_move_confidence", thresholds.get("min_confidence", 0.9)))
    name_conf, move_conf = resolve_confidences(answer)
    if not (answer.get("name_confidence") or answer.get("move_confidence") or answer.get("confidence")):
        verdict.flag("модель не указала уверенность ни в имени, ни в переносе")
    if answer.get("review") is True:
        verdict.flag("модель сама попросила проверку")
    if name_conf < min_name:
        verdict.flag(f"уверенность в имени {name_conf:.2f} ниже порога {min_name:.2f}")
    if move_conf < min_move:
        verdict.flag(f"уверенность в переносе {move_conf:.2f} ниже порога {min_move:.2f}")

    if extracted.force_review:
        verdict.flag(extracted.note or "содержимое не прочитано")
    elif extracted.needs_vision and not (config.get("llm", {}).get("vision")):
        verdict.flag("нужен просмотр изображения/скана глазами")
    if duplicates:
        verdict.flag("есть совпадения по содержимому: " + ", ".join(duplicates[:3]))
    if item.get("error"):
        verdict.flag("ошибка чтения на диске: " + str(item["error"]))

    new_name = clean_file_name(str(answer.get("new_name", "")), extension)
    if not answer.get("new_name"):
        new_name = clean_file_name(old_name, extension)
        verdict.fix("имя оставлено прежнее — модель не предложила")
    if extension and not new_name.lower().endswith(extension):
        new_name = clean_file_name(new_name, extension)
        verdict.fix("расширение восстановлено принудительно")
    new_name = canon_type_block(new_name)
    check_name_date(new_name, extracted, old_name, str(item.get("modified", "")), verdict)

    direction = str(answer.get("direction", "")).strip().lower()
    if direction not in {"вх", "исх", "внутр", ""}:
        verdict.flag(f"направление «{direction}» не из списка (вх/исх/внутр)")
        direction = ""
    doc_date = iso_field(answer.get("doc_date") or answer.get("date"))
    event_start = iso_field(answer.get("event_start"))
    event_end = iso_field(answer.get("event_end"))
    if (answer.get("doc_date") or answer.get("date")) and not doc_date:
        verdict.flag(f"дата документа «{answer.get('doc_date')}» не читается как дата")
    head_date = re.match(r"^(?:19|20)\d{2}(?:-\d{2}){0,2}", new_name)
    if doc_date and head_date and head_date.group(0) != doc_date:
        verdict.flag(f"в имени дата {head_date.group(0)}, а doc_date {doc_date} — что из них из текста?")
    marked = "Вх-" in new_name or new_name.startswith("Вх_")
    marked_out = "Исх-" in new_name or new_name.startswith("Исх_")
    if marked and direction != "вх":
        verdict.fix("в имени блок «Вх» — направление исправлено на входящий")
        direction = "вх"
    elif marked_out and direction != "исх":
        verdict.fix("в имени блок «Исх» — направление исправлено на исходящий")
        direction = "исх"
    elif direction in {"вх", "исх"} and not (marked or marked_out):
        verdict.flag(f"направление «{direction}», но в имени нет блока Вх-/Исх- (номер письма не найден?)")
    if event_start and event_end and event_end < event_start:
        verdict.flag(f"дата события путается: начало {event_start} позже конца {event_end}")
    if event_start and event_end and event_start != event_end and re.match(r"^\d{4}-\d{2}-\d{2}", new_name):
        if new_name[:10] != event_start and doc_date != new_name[:10]:
            verdict.flag("многодневное событие, а имя и папка по не первой дате — сверьте пункт «МНОГОДНЕВНЫЕ»")
    years = [doc_date[:4], event_start[:4], event_end[:4]] + list(extracted.years)

    if extracted.meta.get("несколько документов"):
        verdict.flag("в файле несколько документов: " + str(extracted.meta["несколько документов"]))

    target = normalize_target(str(answer.get("target_path", "")), config, index, verdict)
    target = strip_file_from_target(target, new_name, old_name, verdict)
    target = canonicalize_target(target, config, verdict)
    target = resolve_path(target, index, verdict)
    check_non_event_folder(target, config, verdict)
    # нестыковка года: целевая папка названа годом, а документ датирован другим
    doc_year = (doc_date or "")[:4]
    years_ok = {y for y in (doc_year, (event_start or "")[:4]) if y}
    if years_ok and target:
        for seg in target.split("/"):
            m_year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", seg)
            if m_year and m_year.group(0) not in years_ok:
                verdict.flag(f"папка «{seg}» про {m_year.group(0)}, а документ датирован {doc_date} — год не совпадает")
                break
    if target:
        parts = [p for p in target.split("/") if p]
        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        missing = [p for p in prefixes if p not in index.by_path]
        if missing:
            parent = "/".join(parts[:-1])
            found = index.find_existing(parent, parts[-1])
            if found and found != target:
                verdict.fix(f"такая папка уже есть как «{found}» — путь заменён")
                target = found
            else:
                known = len(prefixes) - len(missing)
                if len(missing) > 1:
                    # цепочка глубже одного уровня: первый недостающий сегмент мог
                    # просто не совпасть с уже существующей папкой — стыкуем к ней
                    dock = index.find_existing("/".join(parts[:known]), parts[known])
                    if dock and dock != parts[known]:
                        target = "/".join([dock, *parts[known + 1:]])
                        verdict.fix(f"цепочка пристыкована: «{parts[known][:40]}» — это уже «{dock}»")
                        parts = [p for p in target.split("/") if p]
                        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
                        missing = [p for p in prefixes if p not in index.by_path]
                        known = len(prefixes) - len(missing)
                        if not missing:
                            found = ""
                if missing:
                    known_depth = known
                    cap = float(config.get("thresholds", {}).get("new_folder_max_confidence", 0.8))
                    # Год/месяц/датированное событие ПОД существующей папкой события — это
                    # схема («Мероприятия/<Событие>/ГГГГ»), а не выдумка: не наказываем review.
                    parent_parts = parts[:-1]
                    scheme_year = (
                        len(missing) == 1
                        and re.match(r"^\d{4}(-\d{1,2}(-\d{1,2})?)?(_|$)", parts[-1])
                        and len(parent_parts) >= 3
                        and fold(parent_parts[1]) == fold("Мероприятия")
                        and "/".join(parent_parts) in index.by_path
                    )
                    if scheme_year:
                        verdict.fix(f"создаётся папка «{parts[-1]}» по схеме под существующим событием")
                    else:
                        if known_depth < 1:
                            verdict.flag("целевой ветки не существует вовсе: " + target)
                        elif len(missing) > 1:
                            verdict.flag(f"создаётся цепочка из {len(missing)} новых папок: {missing[-1]}")
                        else:
                            verdict.fix("создаётся одна новая папка «%s»" % parts[-1])
                        if move_conf > cap:
                            verdict.flag(
                                f"папка «{parts[-1]}» выдумана — уверенность в переносе снижена с {move_conf:.2f} "
                                f"до {cap:.2f} (имя при этом может быть верным); проверьте, нет ли уже похожей"
                            )
                            move_conf = cap

    current_parent = old_path.rpartition("/")[0]
    operation = "без изменений"
    discard = bool(answer.get("discard"))
    if discard:
        verdict.flag("модель предлагает в корзину: " + str(answer.get("discard_reason") or "нет содержательного текста"))
    elif target and new_name and f"{target}/{new_name}" != old_path:
        if target and target != current_parent and new_name != old_name:
            operation = "перенос + переименование"
        elif target and target != current_parent:
            operation = "перенос"
        elif new_name != old_name:
            operation = "переименование"
    if discard:
        operation = "в корзину"
        target = ""
    if operation == "без изменений":
        verdict.fix("место и имя совпадают с текущими — перенос не нужен")
    if INTAKE_RE.search(target or "") and (not target or target == current_parent):
        verdict.flag("файл остаётся в зоне разбора — каноническое место не определено")

    shortcut = "" if discard else normalize_shortcut(
        str(answer.get("shortcut_to", "")), config, index, verdict, direction, target, years)

    record = {
        "Текущий путь": old_path,
        "Текущее имя": old_name,
        "Тип операции": operation,
        "Новое имя": new_name,
        "Целевой путь": target,
        "Путь ярлыка": shortcut,
        "Уверенность имени": round(name_conf, 2),
        "Уверенность переноса": round(move_conf, 2),
        "Причина": str(answer.get("reason", ""))[:600],
        "Применить": "FALSE",
        "Статус": "проверка" if verdict.review else "готово",
        "Комментарий": "",
        # служебные колонки — не попадают в CSV карты
        "_тип_документа": str(answer.get("doc_type", ""))[:60],
        "_направление": direction,
        "_дата": doc_date,
        "_дата-события": "…".join(value for value in (event_start, event_end) if value),
        "_номер": str(answer.get("reg_number", ""))[:20],
        "_тема": "_".join(new_name.split("_")[2:4])[:120],
        "_событие": str(answer.get("event_name") or answer.get("event") or "")[:120],
        "_вид_содержимого": extracted.kind,
        "_хеш": str(item.get("sha256") or ""),
        "_дубликаты": "; ".join(duplicates[:5]),
        "_прочитано_символов": len(extracted.text),
        "_токены_промпта": (usage or {}).get("prompt_tokens", 0),
        "_время_запроса": (usage or {}).get("seconds", 0),
        "_модель": (usage or {}).get("model", ""),
        "_флаги": "; ".join(verdict.flags),
        "_исправления": "; ".join(verdict.fixed),
        "_выбросить": discard,
        "_причина-выброса": str(answer.get("discard_reason") or "")[:300],
        "_несколько_документов": str(extracted.meta.get("несколько документов", "")),
    }
    if path_conflicts(target, new_name, root) and operation != "без изменений":
        verdict.flag("такое имя уже существует — при переносе получит _v2")
    verdict.record = record
    record["Статус"] = "проверка" if verdict.review else "готово"
    return verdict

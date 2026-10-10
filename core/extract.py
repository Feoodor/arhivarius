"""Извлечение текста и метаданных из файла.

Никаких внешних сервисов: только локальные библиотеки. Если текст извлечь
нельзя (скан, картинка, старый .doc), файл помечается как требующий глазной
проверки или зрения модели — угадывать по имени регламент запрещает.
"""
from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".log", ".ini", ".json", ".xml", ".html", ".htm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}
LEGACY_EXT = {".doc", ".rtf", ".odt", ".wps", ".pages"}
MEDIA_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
OFFICE_EXT = {".docx", ".xlsx", ".xlsm", ".pptx", ".docm"}
IGNORED_PREFIX = re.compile(r"^(~\$|._)")

YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[.\-/ ](\d{1,2})[.\-/ ](\d{4})(?!\d)")
ISO_RE = re.compile(r"(20[0-4]\d|19[89]\d)-(\d{2})-(\d{2})")
RU_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[\s\-.]*(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|август\w*"
    r"|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)[\s,]*(\d{4})",
    re.IGNORECASE,
)
MONTH_STEMS = (
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("ма", 5), ("июн", 6),
    ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12),
)


def month_number(word: str) -> int:
    low = word.lower()
    for stem, number in MONTH_STEMS:
        if low.startswith(stem):
            return number
    return 0


@dataclass
class Extracted:
    text: str = ""
    kind: str = "other"          # text | office | pdf | pdf-scan | image | legacy | empty | error
    needs_vision: bool = False
    force_review: bool = False
    note: str = ""
    years: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    pages: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


def clean_ws(value: str) -> str:
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t\u00a0]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _iso(year: str, month: int, day: int) -> str:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    return f"{int(year):04d}-{month:02d}-{day:02d}"


def pick_dates(text: str) -> tuple[list[str], list[str]]:
    years = sorted(set(YEAR_RE.findall(text)))
    dates: list[str] = []
    for day, month, year in DATE_RE.findall(text):
        iso = _iso(year, int(month), int(day))
        if iso:
            dates.append(iso)
    for day, month_word, year in RU_DATE_RE.findall(text):
        iso = _iso(year, month_number(month_word), int(day))
        if iso:
            dates.append(iso)
    for year, month, day in ISO_RE.findall(text):
        iso = _iso(year, int(month), int(day))
        if iso:
            dates.append(iso)
    return years, sorted(set(dates))[:16]


def read_text_file(path: Path, limit: int) -> str:
    raw = path.read_bytes()[: limit * 8]
    for enc in ("utf-8-sig", "utf-8", "cp1251", "koi8-r", "utf-16", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def docx_text(path: Path) -> str:
    parts = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for member in ("word/document.xml",) + tuple(
            n for n in names if n.startswith("word/header") or n.startswith("word/footerr")
        ):
            if member not in names:
                continue
            xml = archive.read(member).decode("utf-8", errors="replace")
            xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
            xml = re.sub(r"</w:p>|</w:tr>|</w:tc>", "\n", xml)
            xml = re.sub(r"<[^>]+>", "", xml)
            parts.append(html.unescape(xml))
    return "\n".join(parts)


def xlsx_text(path: Path, max_rows: int = 120) -> str:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return ""
    out = []
    try:
        for sheet in wb.worksheets[:6]:
            out.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c not in (None, "")]
                if cells:
                    out.append("\t".join(cells))
                if len(out) > max_rows:
                    break
            if len(out) > max_rows:
                break
    finally:
        wb.close()
    return "\n".join(out)


def pptx_text(path: Path) -> str:
    out = []
    try:
        with zipfile.ZipFile(path) as archive:
            slides = sorted(
                (n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)", n).group(1)),
            )
            for member in slides[:20]:
                xml = archive.read(member).decode("utf-8", errors="replace")
                xml = re.sub(r"</a:p>", "\n", xml)
                out.append(html.unescape(re.sub(r"<[^>]+>", "", xml)))
    except Exception:
        return ""
    return "\n".join(out)


def pdf_text(path: Path, max_pages: int = 12) -> tuple[str, int, bool]:
    """Возвращает (текст, страниц, это_скан)."""
    try:
        import fitz
    except Exception:
        return "", 0, False
    try:
        with fitz.open(path) as doc:
            pages = doc.page_count
            chunk = []
            for i in range(min(pages, max_pages)):
                chunk.append(doc[i].get_text())
            text = "\n".join(chunk)
            images = sum(len(doc[i].get_images(full=True)) for i in range(min(pages, max_pages)))
        scan = len(text.strip()) < 250 and (pages == 0 or images >= pages)
        return text, pages, scan
    except Exception:
        return "", 0, False


def image_meta(path: Path) -> dict:
    meta: dict = {}
    try:
        from PIL import ExifTags, Image

        with Image.open(path) as img:
            meta["width"], meta["height"] = img.size
            meta["format"] = img.format or path.suffix.lstrip(".").upper()
            exif = img.getexif()
            for tag, value in exif.items():
                name = ExifTags.TAGS.get(tag, str(tag))
                if name in ("DateTime", "DateTimeOriginal", "Model", "Make", "ImageDescription") and value:
                    meta[name] = str(value).strip("\x00 ")[:120]
    except Exception:
        pass
    return meta


def head_tail(text: str, limit: int) -> str:
    """Начало важнее, но дата/подпись часто в конце — берём и там, и там."""
    text = clean_ws(text)
    if len(text) <= limit:
        return text
    head = int(limit * 0.72)
    tail = int(limit * 0.2)
    return text[:head] + "\n…[середина пропущена]…\n" + text[-tail:]


DOC_HEADWORDS = (
    "РАПОРТ", "ОТЧЕТ", "ОТЧЁТ", "ПРОШЕНИЕ", "АКТ", "СМЕТ", "ДОГОВОР", "ЗАЯВКА", "ПИСЬМО",
    "ПРИГЛАШЕНИЕ", "БЛАГОДАР", "ПРОТОКОЛ", "ПОЛОЖЕНИЕ", "СПИСОК", "СЦЕНАРИЙ", "ХОДАТАЙСТВ",
    "СОГЛАСИЕ", "ИНФОРМ", "ЦИРКУЛ", "ПЛАН", "СПРАВКА",
)


def multi_doc_markers(text: str) -> list[str]:
    """Признаки того, что в одном файле подшиты несколько документов."""
    markers: list[str] = []
    if not text:
        return markers
    heads = sum(
        1 for line in text.splitlines()
        if 3 <= len(line.strip()) <= 40 and any(line.strip().upper().startswith(word) for word in DOC_HEADWORDS)
    )
    if heads >= 2:
        markers.append(f"заголовков документов: {heads}")
    pages = text.count("\f")
    if pages >= 2:
        markers.append(f"переводов страниц: {pages}")
    if len(re.findall(r"(?i)приложение\s*[:№]", text)) >= 2:
        markers.append("несколько приложений")
    dates = set(re.findall(r"(?:19|20)\d{2}-\d{2}-\d{2}", text))
    if len(dates) >= 3:
        markers.append(f"разных дат: {len(dates)}")
    return markers


def extract(path: Path, content_chars: int = 3200, max_pdf_pages: int = 12,
            ocr: bool = False, media_meta: bool = False) -> Extracted:
    """Прочитать файл и вернуть текст и признаки, достаточные для авто-решения."""
    res = Extracted()
    ext = path.suffix.lower()
    if not path.exists():
        res.kind, res.force_review, res.note = "error", True, "файл не найден на диске"
        return res
    if path.stat().st_size == 0:
        res.kind, res.force_review, res.note = "empty", True, "пустой файл"
        return res

    try:
        if ext in TEXT_EXT:
            res.kind = "text"
            res.text = read_text_file(path, content_chars)
        elif ext == ".docx":
            res.kind = "office"
            res.text = docx_text(path)
        elif ext in {".xlsx", ".xlsm"}:
            res.kind = "office"
            res.text = xlsx_text(path)
        elif ext == ".pptx":
            res.kind = "office"
            res.text = pptx_text(path)
        elif ext == ".pdf":
            text, pages, scan = pdf_text(path, max_pdf_pages)
            res.pages = pages
            res.kind = "pdf-scan" if scan else "pdf"
            res.text = text
            res.needs_vision = scan
            if scan:
                res.note = "PDF без текстового слоя (скан) — нужен просмотр или OCR"
        elif ext in IMAGE_EXT:
            res.kind = "image"
            if ext in (".heic", ".heif"):
                from .plugins import ensure_heif

                ensure_heif()
            res.meta = image_meta(path)
            res.needs_vision = True
            stamp = res.meta.get("DateTimeOriginal") or res.meta.get("DateTime") or ""
            res.text = f"Изображение {res.meta.get('format', ext)} {res.meta.get('width', '?')}x{res.meta.get('height', '?')}. EXIF дата: {stamp or 'нет'}"
            res.note = "фото/картинка"
        elif ext in LEGACY_EXT:
            res.kind = "legacy"
            res.force_review = True
            res.note = f"устаревший формат {ext} — сначала конвертировать в docx/pdf"
        elif ext in MEDIA_EXT:
            res.kind = "media"
            res.needs_vision = False
            if media_meta:
                from .plugins import media_info

                res.meta = media_info(path)
            stamp = res.meta.get("название") or ""
            described = " ".join(f"{k}: {v}" for k, v in res.meta.items())
            res.text = f"Медиафайл {ext}" + (f". {described}" if described else "")
            res.note = "аудио/видео — содержимое не читается" + (f"; название из тегов: {stamp}" if stamp else "")
            res.force_review = True
        else:
            res.kind = "other"
            res.force_review = True
            res.note = f"нераспознанный тип {ext or 'без расширения'}"
    except zipfile.BadZipFile:
        res.kind, res.force_review, res.note = "error", True, "повреждённый контейнер office/pdf"
    except Exception as exc:  # не роняем партию из-за одного файла
        res.kind, res.force_review, res.note = "error", True, f"не прочитан: {type(exc).__name__}: {exc}"

    if ocr and res.kind in ("pdf-scan", "image") and (not res.usable or res.kind == "pdf-scan"):
        from .plugins import ocr_text

        recognized = clean_ws(ocr_text(path) or "")
        if len(recognized) >= 40:
            res.text = recognized
            res.meta["ocr"] = True
            res.force_review = False
            res.needs_vision = False   # текст уже есть — «смотреть глазами» не требуется
            res.note = "текст получен распознаванием — проверьте ключевые данные"
            if res.kind == "pdf-scan":
                res.kind = "pdf-ocr"

    years, dates = pick_dates(res.text + " " + path.name)
    res.years, res.dates = years, dates
    multi = multi_doc_markers(res.text)
    if multi:
        res.meta["несколько документов"] = "; ".join(multi)
        res.note = (res.note + "; " if res.note else "") + "в файле несколько документов — проверьте целиком"
    if res.usable and res.kind != "image":
        res.text = head_tail(res.text, content_chars)
    if not res.usable and res.kind not in ("image", "error", "empty", "legacy"):
        res.force_review = True
        res.note = res.note or "текст не извлечён — нужен глаз"
    return res


def modified_year(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y")

"""Плагины и подготовка диска.

Возможности, которые не нужны всем: OCR сканов, конвертация старых .doc,
чтение метаданных видео/аудио, починка битой кодировки в именах. Каждая
проверяется на месте (импорт библиотеки или наличие программы), устанавливается
командой pip из окна программы и включается переключателем в config.json.

Ничего из этого не удаляет файлы: конвертация создаёт новый файл рядом,
переименование даёт суффикс при конфликте, отчёт о битых только список.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

MAGIC = {
    b"PK\x03\x04": "zip/office",
    b"%PDF": "pdf",
    b"\xd0\xcf\x11\xe0": "ole (старый doc/xls)",
    b"\x89PNG": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF8": "gif",
    b"BM": "bmp",
    b"{\\rtf": "rtf",
    b"ID3": "mp3",
    b"\x1aE\xdf\xa3": "matroska/webm",
    b"OggS": "ogg",
    b"RIFF": "riff (wav/avi)",
}
NEED_ZIP = {".docx", ".xlsx", ".pptx", ".xlsm", ".docm", ".odt", ".ods", ".odp"}
NEED_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
NEED_PDF = {".pdf"}
LEGACY_DOC = {".doc", ".rtf", ".odt", ".wps"}
# Ярлыки документов Google Drive (.gsheet, .gform, .gscript и т.п.): такой файл всегда
# 0 байт — это ссылка на облачный документ, а не пустышка и не мусор.
GOOGLE_SHORTCUT = {".gdoc", ".gsheet", ".gslides", ".gform", ".gdraw", ".gsite", ".gftx",
                   ".gem", ".gscript", ".gappsscript", ".gslides"}


@dataclass
class Plugin:
    id: str
    name: str
    purpose: str
    modules: tuple[str, ...] = ()      # что проверять импортом
    packages: tuple[str, ...] = ()     # что ставить через pip
    program: str = ""
    enabled_key: str = ""
    note: str = ""

    def installed(self, config: dict | None = None) -> tuple[bool, str]:
        for module in self.modules:
            if importlib.util.find_spec(module) is None:
                return False, f"нет модуля {module}"
        if self.program:
            found = find_program(self.program, config)
            if not found:
                return False, (f"не найдена программа {self.program} ни в PATH, ни в типовых папках — "
                               "установите её и при необходимости укажите путь в настройках")
            return True, f"{self.program}: {found}"
        return True, "готово"

    def install_command(self) -> list[str]:
        return [sys.executable, "-m", "pip", "install", *self.packages] if self.packages else []


PROGRAM_HINTS = {
    "tesseract": [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
        r"%ProgramFiles%\Tesseract-OCR\tesseract.exe",
    ],
    "ffprobe": [r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffprobe.exe", r"C:\ffmpeg\bin\ffprobe.exe"],
    "soffice": [r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"],
}
CONFIG_KEYS = {"tesseract": "tesseract_cmd", "ffprobe": "ffprobe_cmd", "soffice": "soffice_cmd"}


def set_config(config: dict | None) -> None:
    """Запомнить настройки: внешние вызовы (из extract) не всегда несут config."""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = config or {}


_ACTIVE_CONFIG: dict = {}


def find_program(name: str, config: dict | None = None) -> str:
    """Где лежит программа: явный путь в настройках → PATH → типовые папки установки."""
    if not name:
        return ""
    config = config if config is not None else _ACTIVE_CONFIG
    key = CONFIG_KEYS.get(name)
    if config and key:
        plugins_cfg = config.get("plugins", {})
        explicit = str(plugins_cfg.get(key) or "").strip().strip('"')
        if explicit and Path(explicit).is_file():
            return explicit
    which = shutil.which(name) or shutil.which(name + ".exe")
    if which:
        return which
    candidates = list(PROGRAM_HINTS.get(name, []))
    if name == "tesseract":
        candidates.append(str(Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe"))
    for raw in candidates:
        path = Path(raw) if "%" not in raw else Path(expandvars(raw))
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    for raw in candidates:
        try:
            parent = (Path(expandvars(raw)) if "%" in raw else Path(raw)).parent
        except OSError:
            continue
        if not parent.is_dir():
            continue
        for hit in sorted(parent.glob(f"*{name}*")):
            if (hit.is_file() and hit.suffix.lower() == ".exe"
                    and not hit.name.casefold().startswith("unins")):
                return str(hit)
    return ""


def expandvars(raw: str) -> str:
    import os

    return os.path.expandvars(os.path.expanduser(raw))


def program_version(name: str, config: dict | None = None) -> tuple[bool, str]:
    """Запустить найденную программу с --version: честная проверка, а не «в PATH ли»."""
    path = find_program(name, config)
    if not path:
        return False, f"{name}: не найдена (PATH и типовые папки пусты)"
    try:
        process = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=25)
    except Exception as exc:
        return False, f"{path}: не запустилась ({type(exc).__name__}: {exc})"
    out = (process.stdout or "") + (process.stderr or "")
    first = next((line.strip() for line in out.splitlines() if line.strip()), "без вывода")
    if process.returncode != 0:
        return False, f"{path}: код {process.returncode} — {first}"
    return True, f"{first}  ({path})"


def registry() -> list[Plugin]:
    return [
        Plugin(
            id="ocr",
            name="Распознавание текста (OCR)",
            purpose="Читает сканы PDF и фотографии: вместо «нужен глаз» файл получает текст и может быть разобран.",
            modules=("pytesseract", "PIL"),
            packages=("pytesseract", "Pillow"),
            program="tesseract",
            enabled_key="ocr",
            note="pip ставит только обвязку, нужен сам движок: winget install UB-Mannheim.TesseractOCR "
                 "--override \"/S /L=ru rus\" (или установщик с галочкой Additional language data → Russian). "
                 "Если tesseract.exe не в PATH — впишите путь в настройках (поле «Путь к tesseract.exe») "
                 "и нажмите «Проверить выбранное».",
        ),
        Plugin(
            id="doc_convert",
            name="Конвертация старых форматов",
            purpose="Преобразует .doc, .rtf, .odt в .docx, чтобы их тоже читала модель. Word ищет сам, иначе LibreOffice.",
            modules=("win32com",),
            packages=("pywin32",),
            enabled_key="doc_convert",
            note="Работает через Microsoft Word (pywin32) или, если Word нет, через LibreOffice (soffice в PATH).",
        ),
        Plugin(
            id="media_info",
            name="Метаданные фото, аудио и видео",
            purpose="Достаёт дату съёмки, разрешение, длительность — это подсказка для имени и папки.",
            modules=("PIL",),
            packages=("Pillow",),
            program="ffprobe",
            enabled_key="media_info",
            note="Для видео и аудио нужен ffmpeg/ffprobe в PATH (winget install Gyan.FFmpeg); фото читаются и без него.",
        ),
        Plugin(
            id="name_repair",
            name="Починка битой кодировки имён",
            purpose="Возвращает нормальные кириллические имена папок и файлов, приехавших из архивов с неверной кодировкой.",
            enabled_key="name_repair",
            note="Работает без внешних библиотек; переименование только по вашему подтверждению, конфликтов не затирает.",
        ),
        Plugin(
            id="broken_check",
            name="Поиск битых файлов",
            purpose="Находит пустые, обрезанные и не соответствующие расширению файлы, чтобы убрать их из разбора.",
            note="Встроено, ничего устанавливать не нужно.",
        ),
        Plugin(
            id="quarantine",
            name="Корзина отдела",
            purpose="Собирает кандидатов на выброс (пустые, битые, дубли, мнение модели) и переносит в отдельную папку — "
                   "оттуда вы удаляете вручную. Сама программа файлы не стирает.",
            enabled_key="quarantine",
            note="Ничего не устанавливается. Папка задаётся в настройках (quarantine_dir).",
        ),
        Plugin(
            id="pdf_text",
            name="Чтение текстовых PDF",
            purpose="Извлекает текст из PDF без распознавания.",
            modules=("fitz",),
            packages=("PyMuPDF",),
            note="Ставится одной командой pip install PyMuPDF.",
        ),
        Plugin(
            id="heif",
            name="Фотографии iPhone (HEIC/HEIF)",
            purpose="Без него .heic не открывается: Pillow видит файл, но не умеет его декодировать.",
            modules=("pillow_heif",),
            packages=("pillow-heif",),
            enabled_key="heif",
            note="Ставится одной командой pip install pillow-heif — после этого .heic читаются как обычные фото.",
        ),
        Plugin(
            id="xlsx_read",
            name="Чтение таблиц XLSX",
            purpose="Достаёт содержимое листов для разбора отчётов и смет.",
            modules=("openpyxl",),
            packages=("openpyxl",),
        ),
    ]


def status(config: dict) -> list[dict]:
    plugins_cfg = config.get("plugins", {})
    out = []
    for plugin in registry():
        installed, detail = plugin.installed(config)
        out.append({
            "id": plugin.id,
            "название": plugin.name,
            "зачем": plugin.purpose,
            "установлено": installed,
            "подробно": detail,
            "включено": bool(plugins_cfg.get(plugin.enabled_key)) if plugin.enabled_key else None,
            "команда": " ".join(plugin.install_command()) or (
                f"winget install — см. примечание; путь можно указать в настройках" if plugin.program else "—"),
            "проверка": f"{plugin.program} --version" if plugin.program else "готово",
            "примечание": plugin.note,
        })
    return out


def verify(plugin_id: str, config: dict | None = None, log=print) -> list[str]:
    """Честная проверка возможности: и библиотека, и внешняя программа по версии."""
    plugin = next((p for p in registry() if p.id == plugin_id), None)
    if plugin is None:
        return [f"неизвестная возможность {plugin_id}"]
    lines: list[str] = []
    for module in plugin.modules:
        found = importlib.util.find_spec(module) is not None
        lines.append(f"модуль {module}: {'установлен' if found else 'НЕ найден'}")
    if plugin.program:
        ok, detail = program_version(plugin.program, config)
        lines.append(f"программа {plugin.program}: {'отвечает' if ok else 'нет'} — {detail}")
    if not plugin.modules and not plugin.program:
        lines.append("встроено в программу, проверять нечего")
    for line in lines:
        log("  " + line)
    return lines


def install(plugin_id: str, log=print, config: dict | None = None) -> bool:
    plugin = next((p for p in registry() if p.id == plugin_id), None)
    if plugin is None:
        log(f"неизвестный плагин {plugin_id}")
        return False
    command = plugin.install_command()
    if not command:
        log(f"{plugin.name}: библиотек для установки не требуется. {plugin.note}")
        return False
    log("выполняю: " + " ".join(command))
    try:
        process = subprocess.run(command, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        log("pip молчал 10 минут — прервано. Проверьте интернет или установите вручную.")
        return False
    for line in (process.stdout or "").splitlines()[-12:]:
        log("  " + line)
    if process.returncode != 0:
        for line in (process.stderr or "").splitlines()[-8:]:
            log("  ! " + line)
        log(f"установка не удалась (код {process.returncode})")
        return False
    ok, detail = plugin.installed(config)
    log(f"проверка после установки: {detail}")
    if plugin.program:
        alive, version = program_version(plugin.program, config)
        log(("  программа найдена: " if alive else "  программы нет: ") + version)
    if not ok and plugin.note:
        log(f"  нужно дополнительно: {plugin.note}")
    return ok


# ------------------------------------------------------------------ битые файлы
def check_file(path: Path) -> str:
    """Пусто, обрезан или не то, чем притворяется. Пустая строка — вроде целое."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"не открыть ({type(exc).__name__})"
    ext = path.suffix.lower()
    if size == 0:
        return "" if ext in GOOGLE_SHORTCUT else "пустой файл"
    try:
        with path.open("rb") as stream:
            head = stream.read(8)
    except OSError as exc:
        return f"не читается ({type(exc).__name__})"
    known = next((label for magic, label in MAGIC.items() if head.startswith(magic)), "")
    if ext in NEED_ZIP:
        if not head.startswith(b"PK\x03\x04"):
            return f"не zip-контейнер (начинается с {head[:4]!r})"
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
            if ext == ".docx" and "word/document.xml" not in names:
                return "в docx нет word/document.xml"
            if ext in {".xlsx", ".xlsm"} and not any(n.startswith("xl/") for n in names):
                return "в xlsx нет данных листа"
        except zipfile.BadZipFile:
            return "битый zip-контейнер"
        return ""
    if ext in NEED_PDF:
        if not head.startswith(b"%PDF"):
            return "не PDF"
        try:
            tail = path.read_bytes()[-1024:]
        except OSError:
            return ""
        return "" if b"%%EOF" in tail else "обрезан (нет конца файла)"
    if ext in NEED_IMAGE:
        if not known:
            return f"расширение {ext}, но содержимое другое"
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
        except Exception:
            return "изображение не раскрывается"
        return ""
    if ext in LEGACY_DOC and not known:
        return f"расширение {ext}, сигнатура не распознана"
    return ""


def find_broken(config: dict, limit: int = 0, progress=None) -> dict:
    """Список подозрительных файлов в CSV. Ничего не переименовывает и не удаляет."""
    root: Path = config["scan_root"]
    output: Path = config["output_path"]
    rows = []
    seen = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.casefold()
        if name in config["excluded_names"] or any(name.startswith(p) for p in config["excluded_prefixes"]):
            continue
        seen += 1
        if progress and seen % 250 == 0:
            progress(path.name, seen)
        reason = check_file(path)
        if reason:
            rows.append({
                "файл": path.relative_to(root).as_posix(),
                "размер": path.stat().st_size if path.exists() else 0,
                "проблема": reason,
                "что делать": "открыть глазами; если не читается — в 90_Завершённые или на проверку",
            })
        if limit and len(rows) >= limit:
            break
    target = output / "broken_files.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    import csv

    with target.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["файл", "размер", "проблема", "что делать"], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return {"проверено": seen, "подозрительных": len(rows), "файл": str(target)}


# --------------------------------------------------------------- битая кодировка
def fix_mojibake(name: str) -> str:
    """Попытка вернуть кириллицу, съехавшую при распаковке архива в неверной кодировке."""
    if all(ord(ch) < 128 for ch in name):
        return ""
    for source, target in (("cp437", "cp866"), ("cp1252", "cp1251"), ("latin1", "cp866"), ("latin1", "cp1251")):
        try:
            candidate = name.encode(source, errors="strict").decode(target, errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        cyrillic = sum(1 for ch in candidate if "а" <= ch.lower() <= "я")
        junk = sum(1 for ch in candidate if ch in "¿¡ΓÑα⌐öñâ∩úÆÉäèîàï¢æçêëìíïµ¼½¾·░▒│")
        if cyrillic >= 3 and junk == 0 and candidate != name:
            return candidate
    return ""


def name_repairs(config: dict) -> list[dict]:
    """Что можно починить в именах (только предложение, без изменений)."""
    root: Path = config["scan_root"]
    rows = []
    for path in sorted(root.rglob("*")):
        if path in (root,):
            continue
        fixed = fix_mojibake(path.name)
        if not fixed:
            continue
        parent = path.parent
        try:
            relative = parent.relative_to(root).as_posix()
        except ValueError:
            continue
        target = parent / fixed
        rows.append({
            "объект": "папка" if path.is_dir() else "файл",
            "сейчас": path.name,
            "предлагается": fixed,
            "где": relative if relative != "." else "(в корне)",
            "свободно": "да" if not target.exists() else "нет — конфликт",
        })
    return rows


def apply_name_repairs(config: dict, log=print) -> dict:
    """Переименовать починенные имена. Конфликты пропускаются, удаления нет."""
    root: Path = config["scan_root"]
    renamed = skipped = 0
    for path in sorted(root.rglob("*"), key=lambda p: (-len(p.parts), str(p))):
        if path == root or not path.exists():
            continue
        fixed = fix_mojibake(path.name)
        if not fixed:
            continue
        target = path.with_name(fixed)
        if target.exists():
            skipped += 1
            log(f"пропуск (уже есть {fixed}): {path.name}")
            continue
        try:
            path.rename(target)
            renamed += 1
            log(f"переименовано: {path.name} → {fixed}")
        except OSError as exc:
            skipped += 1
            log(f"не вышло ({exc}): {path.name}")
    return {"переименовано": renamed, "пропущено": skipped}


_HEIF_READY: bool | None = None


def ensure_heif() -> bool:
    """Подключить декодер HEIC, если библиотека установлена (один раз за запуск)."""
    global _HEIF_READY
    if _HEIF_READY is not None:
        return _HEIF_READY
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _HEIF_READY = True
    except Exception:
        _HEIF_READY = False
    return _HEIF_READY


# ------------------------------------------------------------------ OCR и медиа
def ocr_languages(config: dict | None = None) -> list[str]:
    """Какие языки реально есть у установленного Tesseract."""
    engine = find_program("tesseract", config)
    if not engine:
        return []
    try:
        process = subprocess.run([engine, "--list-langs"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=25)
    except Exception:
        return []
    lines = (process.stdout or "") + (process.stderr or "")
    return [line.strip() for line in lines.splitlines()[1:] if line.strip() and not line.startswith("List of")]


def ocr_status(config: dict | None = None) -> tuple[bool, str]:
    """Готов ли OCR и почему нет — человеческим языком."""
    if importlib.util.find_spec("pytesseract") is None:
        return False, "нет библиотеки pytesseract — установите плагин «Распознавание текста (OCR)»"
    engine = find_program("tesseract", config)
    if not engine:
        return False, ("не найден tesseract.exe: ни в PATH, ни в Program Files. Установите "
                       "UB-Mannheim Tesseract OCR и при необходимости укажите путь в настройках")
    langs = ocr_languages(config)
    if not langs:
        return True, f"{engine} — языки не удалось прочитать"
    if "rus" not in langs:
        return False, f"{engine} найден, но русского языка нет (есть: {', '.join(langs[:8])}). " \
                      "Переустановите с галочкой Additional language data → Russian"
    return True, f"{engine}; языков: {len(langs)}, rus есть"


def ocr_text(path: Path, lang: str = "rus+eng", config: dict | None = None, log=None) -> str:
    """Распознать текст картинки или страницы PDF. Пустая строка, если OCR недоступен."""
    import tempfile

    say = log or (lambda message: None)
    ready, why = ocr_status(config)
    if not ready:
        say("OCR пропущен: " + why)
        return ""
    wanted = [part for part in lang.split("+") if part]
    have = ocr_languages(config)
    if have:
        usable = [part for part in wanted if part in have] or [p for p in wanted if p == "eng"] or have[:1]
        if usable != wanted:
            say(f"OCR: язык(и) {', '.join(wanted)} есть не все, распознаю как {'+'.join(usable) or 'авто'}")
        lang = "+".join(usable) or lang
    images: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix="mo_ocr_") as tmp:
            if path.suffix.lower() == ".pdf":
                import fitz

                with fitz.open(path) as doc:
                    for number, page in enumerate(list(doc)[:3]):
                        out = Path(tmp) / f"page{number}.png"
                        page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(out)
                        images.append(out)
            else:
                images.append(path)
            if not images:
                return ""
            import pytesseract
            from PIL import Image

            engine = find_program("tesseract", config)
            if engine:
                pytesseract.pytesseract.tesseract_cmd = engine
            chunks = []
            for image_path in images:
                with Image.open(image_path) as image:
                    chunks.append(pytesseract.image_to_string(image, lang=lang))
            return "\n".join(chunk for chunk in chunks if chunk.strip())
    except Exception as exc:
        say(f"OCR не сработал ({type(exc).__name__}): {str(exc)[:160]}")
        return ""


def media_info(path: Path, config: dict | None = None) -> dict:
    """Длительность, битрейт, кодек для аудио и видео через ffprobe."""
    probe = find_program("ffprobe", config)
    if not probe:
        return {}
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=25, encoding="utf-8", errors="replace",
        )
        data = json.loads(result.stdout or "{}")
    except Exception:
        return {}
    out: dict = {}
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        out["длительность"] = f"{float(fmt['duration']) / 60:.1f} мин"
    if fmt.get("tags", {}).get("title"):
        out["название"] = str(fmt["tags"]["title"])[:120]
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not out.get("разрешение"):
            out["разрешение"] = f"{stream.get('width')}x{stream.get('height')}"
        if stream.get("codec_type") == "audio" and not out.get("аудио"):
            out["аудио"] = stream.get("codec_name", "")
    return out


# ---------------------------------------------------------------- конвертация
def convert_legacy(config: dict, apply: bool = False, limit: int = 50, progress=None) -> dict:
    """Старые .doc/.rtf/.odt → .docx рядом с оригиналом. Оригиналы не трогаются."""
    root: Path = config["scan_root"]
    targets = [
        path for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in LEGACY_DOC
    ]
    if limit:
        targets = targets[:limit]
    word = None
    soffice = find_program("soffice", config) or shutil.which("libreoffice")
    if apply and not soffice:
        try:
            import pythoncom  # noqa: F401
            import win32com.client

            pythoncom.CoInitialize()
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
        except Exception:
            word = None
    done, failed, skipped = [], [], []
    try:
        for number, path in enumerate(targets, 1):
            if progress:
                progress(path.name, number)
            out = path.with_suffix(".docx")
            if out.exists():
                skipped.append(str(path.relative_to(root).as_posix()))
                continue
            if not apply:
                done.append(str(path.relative_to(root).as_posix()))
                continue
            try:
                if soffice:
                    subprocess.run(
                        [soffice, "--headless", "--convert-to", "docx", "--outdir", str(path.parent), str(path)],
                        capture_output=True, text=True, timeout=180, check=True,
                    )
                elif word is not None:
                    document = word.Documents.Open(str(path))
                    document.SaveAs2(str(out), FileFormat=16)
                    document.Close(False)
                else:
                    raise RuntimeError("нет ни Word, ни LibreOffice")
                done.append(str(path.relative_to(root).as_posix()))
            except Exception as exc:
                failed.append(f"{path.name}: {type(exc).__name__}: {exc}")
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
    report = config["output_path"] / f"convert_{datetime.now():%Y%m%d-%H%M%S}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"готово": done, "пропущено": skipped, "ошибки": failed,
                                  "применено": apply}, ensure_ascii=False, indent=2), "utf-8")
    return {"найдено": len(targets), "конвертировано": len(done) if apply else 0,
            "ждут": len(done), "ошибок": len(failed), "отчёт": str(report)}


def convertible_originals(config: dict) -> list[dict]:
    """Старые .doc/.rtf/.odt, рядом с которыми уже лежит сконвертированный .docx.

    Строки в формате корзины: кнопка «Убрать сконвертированные оригиналы»
    переносит их в папку корзины (не удаляет), решение за человеком.
    """
    root: Path = config["scan_root"]
    rows: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LEGACY_DOC:
            continue
        if not path.with_suffix(".docx").exists():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({
            "Путь": path.relative_to(root).as_posix(),
            "Имя": path.name,
            "Размер": int(stat.st_size),
            "Изменён": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")[:19],
            "Причина": f"есть {path.with_suffix('.docx').name} — оригинал сконвертирован",
            "Уверенность": "конвертирован",
            "Отметить": "TRUE",
        })
    return rows

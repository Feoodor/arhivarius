"""Окно программы: мягкий светлый интерфейс в духе Telegram и Notion.

Отличия от прежней версии: градиентный фон, «стеклянные» карточки, кнопки-пилюли
с анимацией наведения и нажатия, скользящий переключатель режимов, плавная смена
страниц и вертикальный скролл на каждой вкладке (настройки больше не прячутся).

Логика прежняя: тяжёлые операции в рабочем потоке, окно обновляется через
очередь событий. Программа не удаляет файлы — переносит утверждённое, а лишнее
предлагает убрать в папку корзины, откуда вы удаляете его вручную.
"""
from __future__ import annotations

import json
import queue
import threading
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from core import applier, cleanup, mapio, plugins, scanner
from core.config_loader import MAP_NAME, SUGGESTIONS_NAME, load_config, save_config
from core.folders import FolderIndex
from core.pipeline import Analyzer, Filters, build_prompt_preview
from core.prompts import templates_info, reset_template
from ui import theme as T
from ui.viewer import open_viewer

PAGES = [
    ("home", "🏠 Обзор"),
    ("map", "🗂 Карта"),
    ("tree", "🌳 Структура"),
    ("prompt", "✨ Промпт"),
    ("trash", "🗑 Корзина"),
    ("plugins", "🧩 Плагины"),
    ("guide", "📖 Инструкция"),
    ("settings", "⚙️ Настройки"),
    ("log", "📜 Журнал"),
]

MODES = ("🧹 Разбор завалов", "🧭 Ревизия структуры", "🔍 Вся система", "🪜 Система → потом разбор")
MODE_KEYS = ("завалы", "ревизия структуры", "проверка системы", "система-затем-разбор")
MODE_NOTES = {
    "завалы": "Разбираем временную папку «Разобрать»: модель ищет каждому файлу постоянное место.",
    "ревизия структуры": "Ревизия постоянной структуры: файлы уже на местах, сверяем имена и типы. "
                         "Зона «Разобрать» в очереди не участвует.",
    "проверка системы": "Проверка всей системы: ищем ошибки раскладки — не тот раздел, дубли, чужие материалы.",
    "система-затем-разбор": "Сначала проходим по существующей системе папок и наводим в ней порядок, "
                            "а файлы из «Разобрать» добавляются в очередь после них. Так новое не попадёт "
                            "в старые завалы.",
}

_GUIDE_FILE = Path(__file__).resolve().parent / "guide.txt"
GUIDE_TEXT = _GUIDE_FILE.read_text("utf-8") if _GUIDE_FILE.exists() else "Подробный порядок — в README.md."


def fmt_size(value: int) -> str:
    size = float(value or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


class _MenuEvent:
    """Меню вызывается без «настоящего» события — подставляем виджет-цель."""

    def __init__(self, app):
        widget = getattr(app, "_ctx_target", None)
        try:
            widget = widget if widget is not None else (app.focus_get() or app)
        except (tk.TclError, KeyError):
            widget = app
        self.widget = widget


def open_path(path: Path) -> None:
    import os
    import subprocess
    import sys

    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class OrganizerApp(tk.Tk):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        from ui import i18n as I
        I.set_lang(str(config.get("ui_lang", "ru")))
        self.title("Архивариус")
        try:
            icon = Path(__file__).resolve().parent.parent / "logo.png"
            if icon.exists():
                self.iconphoto(True, tk.PhotoImage(file=icon))
        except tk.TclError:
            pass
        self.minsize(980, 620)
        self.configure(bg=T.BG_TOP)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._fit_screen()

        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.analyzer: Analyzer | None = None
        self.snapshot: dict | None = None
        self.index: FolderIndex | None = None
        self.rows: list[dict] = []
        self.filtered: list[dict] = []
        self.trash_rows: list[dict] = []
        self.details: dict[str, dict] = {}
        self.showing_history = False
        self._sort_field, self._sort_reverse = "", False

        self._style()
        self._build()
        self.after(90, self._drain)
        self.after(250, self.refresh_all)

    # ------------------------------------------------------------------ стиль
    def _style(self) -> None:
        import tkinter.font as tkfont
        for fname, size in (("TkDefaultFont", 10), ("TkTextFont", 10), ("TkMenuFont", 10),
                            ("TkHeadingFont", 10), ("TkIconFont", 10), ("TkFixedFont", 10)):
            try:
                tkfont.nametofont(fname).configure(size=size)
            except tk.TclError:
                pass
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=34, background=T.CARD,
                        fieldbackground=T.CARD, borderwidth=0, foreground=T.INK)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10), padding=(6, 9),
                        background=T.CARD_SOFT, foreground=T.MUTED, borderwidth=0)
        style.map("Treeview", background=[("selected", T.ACCENT_SOFT)], foreground=[("selected", T.INK)])
        style.configure("TScrollbar", background=T.BORDER_STRONG, troughcolor=T.CARD_SOFT,
                        bordercolor=T.CARD_SOFT, arrowcolor=T.MUTED)
        style.configure("Horizontal.TProgressbar", background=T.ACCENT, troughcolor="#d7dde6", borderwidth=0)
        # поля и выбор — в тему, чтобы рядом с пилюлями не было «серого нативного»
        for cls in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(cls, fieldbackground=T.CARD_SOFT, background=T.CARD_SOFT,
                            foreground=T.INK, bordercolor=T.BORDER, lightcolor=T.BORDER,
                            darkcolor=T.BORDER, arrowcolor=T.MUTED, padding=6, relief="flat")
            style.map(cls,
                      fieldbackground=[("focus", T.CARD), ("disabled", T.CARD_SOFT)],
                      bordercolor=[("focus", T.ACCENT)],
                      lightcolor=[("focus", T.ACCENT)],
                      darkcolor=[("focus", T.ACCENT)])
        style.configure("TCheckbutton", background=T.CARD, foreground=T.INK, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", T.CARD)],
                  indicatorcolor=[("selected", T.ACCENT)])
        style.configure("TNotebook", background=T.BG_TOP, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", font=("Segoe UI Semibold", 10), padding=(14, 7),
                        background=T.CARD_SOFT, foreground=T.MUTED)
        style.map("TNotebook.Tab", background=[("selected", T.CARD)],
                  foreground=[("selected", T.INK)])
        style.configure("TPanedwindow", background=T.BORDER)
        style.configure("Horizontal.TPanedwindow.Sash", background=T.BORDER, width=6)

    def set_dot(self, color: str) -> None:
        """Векторный индикатор состояния Ollama: перерисовка гладким кружком."""
        try:
            self.dot.delete("all")
            T.smooth_circle(self.dot, 8, 8, 6, color)
        except tk.TclError:
            pass

    def _fit_screen(self) -> None:
        """Окно никогда не должно выходить за экран: иначе правый край и низ «отрезаются»."""
        self.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = max(980, min(1400, screen_w - 40))
        height = max(620, min(880, screen_h - 70))
        left = max(0, (screen_w - width) // 2)
        top = max(0, (screen_h - height) // 2 - 10)
        self.geometry(f"{width}x{height}+{left}+{top}")

    def _wrap(self, label: tk.Label, extra: int = 0) -> tk.Label:
        """Длина строки подписки = ширина контейнера: текст не уезжает за правый край."""
        def resync(event=None) -> None:
            width = max(280, int((event.width if event else label.winfo_width())) - 12 - extra)
            try:
                label.configure(wraplength=width)
            except tk.TclError:
                pass

        label.bind("<Configure>", resync)
        self.bind("<Configure>", lambda _e: self.after(60, resync), add="+")
        resync()
        return label

    # ------------------------------------------------------------------ сборка
    def _build(self) -> None:
        self.gradient = T.Gradient(self)
        self.gradient.place(x=0, y=0, relwidth=1, relheight=1)

        header = tk.Frame(self, bg=T.BG_TOP)
        header.place(x=24, y=14, relwidth=1, width=-48, height=90)
        title = tk.Frame(header, bg=T.BG_TOP)
        title.pack(side="left")
        tk.Label(title, text="Архивариус", bg=T.BG_TOP, fg=T.INK,
                 font=("Segoe UI Semibold", 26)).pack(anchor="w")
        tk.Label(title, text="читает содержимое · предлагает имя и папку · переносит только утверждённое "
                             "· отдел настраивается в промпте",
                 bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 11)).pack(anchor="w")
        self.dot = tk.Canvas(header, width=16, height=16, bg=T.BG_TOP, highlightthickness=0)
        self.dot.pack(side="left", padx=(18, 5))
        self.set_dot("#9aa4b2")
        self.ollama_label = tk.Label(header, text="Ollama: проверка…", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 11))
        self.ollama_label.pack(side="left")
        T.Pill(header, "Открыть результаты", command=lambda: open_path(self.config["output_path"]),
               style="ghost", icon="📂").pack(side="right")

        from ui import i18n as I
        self.tabbar = T.TabBar(self, [(k, I.tr(title)) for k, title in PAGES], command=self._show_page, bg=T.BG_TOP)
        self.tabbar.place(x=24, y=108, relwidth=1, width=-48, height=44)

        self.container = tk.Frame(self, bg=T.BG_TOP)
        self.container.place(x=24, y=158, relwidth=1, width=-48, relheight=1, height=-190)
        self.pages: dict[str, T.Scrollable] = {}
        for key, _title in PAGES:
            page = T.Scrollable(self.container, bg=T.BG_TOP)
            page.place(x=0, y=0, relwidth=1, relheight=1)
            self.pages[key] = page

        self._build_home(self.pages["home"].body)
        self._build_map(self.pages["map"].body)
        self._build_tree(self.pages["tree"].body)
        self._build_prompt(self.pages["prompt"].body)
        self._build_trash(self.pages["trash"].body)
        self._build_plugins(self.pages["plugins"].body)
        self._build_guide(self.pages["guide"].body)
        self._build_settings(self.pages["settings"].body)
        self._build_log(self.pages["log"].body)
        self._apply_wrapping()
        self._make_texts_copyable()
        self._enable_clipboard_shortcuts()
        self._show_page("home")

        self.status_var = tk.StringVar(value="Готово")
        status = tk.Frame(self, bg=T.BG_TOP)
        status.place(x=24, rely=1, y=-36, relwidth=1, width=-48, height=28)
        tk.Label(status, textvariable=self.status_var, bg=T.BG_TOP, fg=T.MUTED,
                 font=("Segoe UI", 10)).pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=100)
        self.progress.pack(side="right", fill="x", expand=True, padx=(20, 0))

    def _show_page(self, key: str) -> None:
        # сначала снимаем колесо со всех страниц: bind_all общий, и последний
        # снявший обрубил бы прокрутку активной страницы
        for page in self.pages.values():
            page.grab_wheel(False)
        page = self.pages.get(key)
        if page is None:
            return
        page.place(x=0, y=0, relwidth=1, relheight=1)
        page.tkraise()
        page.scroll_to_top()
        page.grab_wheel(True)          # колесо ловится на всей странице, а не только на фоне
        T.fade_in(page.body, T.ACCENT_SOFT, T.BG_TOP)

    def _card(self, parent, title: str = "", tone: str = T.CARD) -> tk.Frame:
        from ui import i18n as _I
        title = _I.tr(title)
        card = T.Card(parent, padding=14, tone=tone)
        card.pack(fill="x", pady=(0, 12))
        card.inner.pack(fill="x")
        if title:
            tk.Label(card.inner, text=title, bg=tone, fg=T.INK,
                     font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
        return card.inner

    def _descendants(self):
        """walklist() есть не во всех сборках Tk — обходим дерево сами."""
        stack = list(self.winfo_children())
        while stack:
            widget = stack.pop()
            yield widget
            try:
                stack.extend(widget.winfo_children())
            except tk.TclError:
                continue

    def _apply_wrapping(self) -> None:
        """Ни одна подпись не должна упираться в правый край: перенос по фактической ширине."""
        for widget in self._descendants():
            if not isinstance(widget, tk.Label):
                continue
            try:
                if int(widget.cget("wraplength") or 0) <= 0:
                    continue
            except (ValueError, tk.TclError):
                continue

            def resync(event=None, target=widget) -> None:
                width = max(260, int(event.width if event else target.winfo_width()))
                try:
                    target.configure(wraplength=width)
                except tk.TclError:
                    pass

            target = widget
            target.bind("<Configure>", resync)
            target.after(120, resync)

    def _make_texts_copyable(self) -> None:
        """Журнал, инструкция и подробности строки должны копироваться (Ctrl+C).

        disabled-поле в tkinter не выделяется мышью, поэтому поля остаются обычными,
        а ввод перехватывается — случайно «испортить» текст нельзя.
        """
        editable = {id(self.prompt_text)} if hasattr(self, "prompt_text") else set()
        for widget in self._descendants():
            if not isinstance(widget, tk.Text) or id(widget) in editable:
                continue
            try:
                widget.configure(state="normal")
                T.selectable_copy(widget)
            except tk.TclError:
                continue

    def _enable_clipboard_shortcuts(self) -> None:
        """Ctrl+C / Ctrl+V / Ctrl+X / Ctrl+A во всех полях — в tkinter это не по умолчанию."""

        def copy(event):
            w = event.widget
            try:
                if w.winfo_class() in ("Entry", "TEntry", "TCombobox", "Combobox"):
                    try:
                        text = w.selection_get()
                    except tk.TclError:
                        text = w.get()
                else:
                    try:
                        text = w.get("sel.first", "sel.last")
                    except tk.TclError:
                        text = w.get("1.0", "end-1c")
                w.clipboard_clear()
                w.clipboard_append(text)
            except tk.TclError:
                pass
            return "break"

        def paste(event):
            w = event.widget
            try:
                data = w.clipboard_get()
            except tk.TclError:
                return "break"
            cls = w.winfo_class()
            if cls in ("TCombobox", "Combobox") or getattr(w, "_ro_guard", False):
                return "break"                       #ReadOnly-поля не трогаем
            try:
                if cls in ("Entry", "TEntry"):
                    data = (data.splitlines() or [""])[0]
                    try:
                        w.delete("sel.first", "sel.last")
                    except tk.TclError:
                        pass
                    w.insert("insert", data)
                elif cls == "Text":
                    if str(w.cget("state")) == "disabled":
                        return "break"
                    try:
                        w.delete("sel.first", "sel.last")
                    except tk.TclError:
                        pass
                    w.insert("insert", data)
            except tk.TclError:
                pass
            return "break"

        def cut(event):
            w = event.widget
            copy(event)
            if getattr(w, "_ro_guard", False) or w.winfo_class() in ("TCombobox", "Combobox"):
                return "break"
            try:
                if w.winfo_class() == "Text" and str(w.cget("state")) == "disabled":
                    return "break"
                w.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            return "break"

        def select_all(event):
            w = event.widget
            try:
                cls = w.winfo_class()
                if cls in ("Entry", "TEntry", "TCombobox", "Combobox"):
                    w.selection_range(0, "end")
                    w.icursor("end")
                elif cls == "Text":
                    w.tag_add("sel", "1.0", "end-1c")
            except tk.TclError:
                pass
            return "break"

        for cls in ("Text", "Entry", "TEntry", "TCombobox", "Combobox"):
            for seq, fn in (("<Control-c>", copy), ("<Control-C>", copy), ("<Insert>", None),
                            ("<Control-v>", paste), ("<Control-V>", paste),
                            ("<Control-x>", cut), ("<Control-X>", cut),
                            ("<Control-a>", select_all), ("<Control-A>", select_all)):
                if fn is None:
                    continue
                try:
                    self.bind_class(cls, seq, fn, add="+")
                except tk.TclError:
                    continue

        # правый клик — то же самое мышкой: не каждый помнит горячие клавиши
        menu = tk.Menu(self, tearoff=0, bg=T.CARD, fg=T.INK, activebackground=T.ACCENT,
                       activeforeground="#ffffff", relief="flat", borderwidth=1)
        menu.add_command(label="Копировать", command=lambda: copy(_MenuEvent(self)))
        menu.add_command(label="Вставить", command=lambda: paste(_MenuEvent(self)))
        menu.add_command(label="Вырезать", command=lambda: cut(_MenuEvent(self)))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: select_all(_MenuEvent(self)))
        self._ctx_menu = menu

        def show_menu(event):
            self._ctx_target = event.widget
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        for cls in ("Text", "Entry", "TEntry", "TCombobox"):
            try:
                self.bind_class(cls, "<Button-3>", show_menu, add="+")
            except tk.TclError:
                continue

    # ------------------------------------------------------------------- обзор
    def _build_home(self, parent) -> None:
        cards = tk.Frame(parent, bg=T.BG_TOP)
        cards.pack(fill="x")
        self.cards: dict[str, tk.Label] = {}
        from ui import i18n as _I
        for name in ("Файлов", "Папок", "К разбору", "На проверке", "Отмечено", "В истории"):
            card = T.Card(cards, padding=12)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            inner = card.inner
            inner.pack(fill="x")
            value = tk.Label(inner, text="—", bg=T.CARD, fg=T.INK, font=("Segoe UI Semibold", 22))
            value.pack(anchor="w")
            tk.Label(inner, text=_I.tr(name), bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10)).pack(anchor="w")
            self.cards[name] = value

        mode_card = self._card(parent, "Режим работы")
        self.mode = T.Segmented(mode_card, MODES, command=self._mode_changed, bg=T.CARD)
        self.mode.pack(fill="x", ipady=2)
        self.mode_note = tk.Label(mode_card, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10),
                                  wraplength=1180, justify="left", anchor="w")
        self.mode_note.pack(anchor="w", pady=(6, 0))

        flow = tk.Frame(parent, bg=T.BG_TOP)
        flow.pack(fill="x", pady=(12, 0))
        steps_card = T.Card(flow, padding=16)
        side_card = T.Card(flow, padding=16)
        steps_card.pack(side="left", fill="x", expand=True, padx=(0, 12))
        side_card.pack(side="left", fill="x", expand=True)
        steps_card.inner.pack(fill="x")
        side_card.inner.pack(fill="x")

        tk.Label(steps_card.inner, text="Рабочий цикл", bg=T.CARD, fg=T.INK,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
        for number, label, note, command in (
            ("1", "📥 Обновить снимок", "читает дерево и хеши, файлы не трогает", self.start_scan),
            ("2", "✨ Разобрать партию", "один файл — один запрос, можно остановить", self.start_analyze),
            ("3", "👁 Смотреть и утвердить", "открыть файл, поправить, решить", lambda: self._goto("map")),
            ("4", "🧪 Пробный перенос", "показывает список без изменений", self.show_dry_run),
            ("5", "✅ Применить", "переносит, пишет журнал, можно откатить", self.apply_marked),
        ):
            row = tk.Frame(steps_card.inner, bg=T.CARD)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=number, bg=T.ACCENT_SOFT, fg=T.ACCENT_DARK, width=2,
                     font=("Segoe UI Semibold", 12)).pack(side="left", padx=(0, 10))
            text = tk.Frame(row, bg=T.CARD)
            text.pack(side="left", fill="x", expand=True)
            tk.Label(text, text=label, bg=T.CARD, fg=T.INK, font=("Segoe UI Semibold", 11)).pack(anchor="w")
            tk.Label(text, text=note, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10)).pack(anchor="w")
            T.Pill(row, "Выполнить", command=command, style="ghost").pack(side="right")

        tk.Frame(steps_card.inner, bg=T.BORDER, height=1).pack(fill="x", pady=10)
        actions = tk.Frame(steps_card.inner, bg=T.CARD)
        actions.pack(fill="x")
        T.Pill(actions, "⏸ Пауза", command=self.pause_work, style="ghost").pack(side="left")
        T.Pill(actions, "⏹ Стоп", command=self.stop_work, style="danger").pack(side="left", padx=6)
        T.Pill(actions, "♻ Откатить пачку", command=self.undo_last, style="ghost").pack(side="right")

        tk.Label(side_card.inner, text="Быстрый запуск", bg=T.CARD, fg=T.INK,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
        self.quick = {
            "prefix": tk.StringVar(value=str(self.config["llm"].get("default_prefix", ""))),
            "limit": tk.IntVar(value=int(self.config["llm"].get("default_limit", 20))),
            "media": tk.BooleanVar(value=False),
            "vision": tk.BooleanVar(value=bool(self.config["llm"].get("vision", True))),
            "redo": tk.BooleanVar(value=False),
        }
        self._entry_field(side_card.inner, "Папка/префикс от корня сканирования", self.quick["prefix"])
        self._entry_field(side_card.inner, "Файлов в партии", self.quick["limit"])
        for key, label in (("media", "включить фотографии в очередь"),
                           ("vision", "зрение модели для картинок и сканов"),
                           ("redo", "разобрать заново уже обработанные")):
            tk.Checkbutton(side_card.inner, text=label, variable=self.quick[key], bg=T.CARD, fg=T.INK,
                           activebackground=T.CARD, selectcolor=T.CARD_SOFT, anchor="w",
                           font=("Segoe UI", 10)).pack(fill="x", pady=1)
        T.Pill(side_card.inner, "▶ Начать анализ", command=self.start_analyze, style="accent").pack(fill="x", pady=(10, 4))
        self.home_info = tk.Label(side_card.inner, text="", bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10),
                                  wraplength=390, justify="left", anchor="w")
        self.home_info.pack(fill="x", pady=6)

    def _goto(self, key: str) -> None:
        self.tabbar.select(key)

    def _mode_changed(self, index: int, _label: str) -> None:
        self.config.setdefault("queue", {})["mode"] = MODE_KEYS[index]
        save_config(self.config)
        self.config = load_config(self.config["app_dir"])
        self._mode_note()
        self.append_log(f"режим: {MODE_KEYS[index]}", "note")

    def _mode_note(self) -> None:
        if not hasattr(self, "mode"):
            return
        key = MODE_KEYS[self.mode.selected] if self.mode.selected < len(MODE_KEYS) else MODE_KEYS[0]
        self.mode_note.configure(text=MODE_NOTES.get(key, ""))

    # -------------------------------------------------------------------- карта
    def _build_map(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(0, 8))
        T.Pill(bar, "👁 Смотреть и утвердить", command=self.view_selected, style="accent").pack(side="left")
        T.Pill(bar, "✅ Утвердить помеченные", command=self.approve_marked, style="good").pack(side="left", padx=6)
        T.Pill(bar, "Отметить", command=lambda: self.set_apply(True), style="ghost").pack(side="left")
        T.Pill(bar, "Снять", command=lambda: self.set_apply(False), style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "☑ Выделить все", command=self.select_all_rows, style="quiet").pack(side="left")
        T.Pill(bar, "🔄 Обновить", command=self.load_map, style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "💾 Сохранить", command=self.save_map, style="ghost").pack(side="right")

        tools = tk.Frame(parent, bg=T.BG_TOP)
        tools.pack(fill="x", pady=(0, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.filter_rows())
        tk.Label(tools, text="Поиск", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left")
        tk.Entry(tools, textvariable=self.search_var, width=26, bg=T.CARD, fg=T.INK, relief="flat",
                 highlightthickness=1, highlightbackground=T.BORDER, highlightcolor=T.ACCENT,
                 insertbackground=T.INK, font=("Segoe UI", 11)).pack(side="left", padx=(6, 14), ipady=4)
        self.status_filter = tk.StringVar(value="все")
        tk.Label(tools, text="Статус", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left")
        ttk.Combobox(tools, textvariable=self.status_filter, width=12, state="readonly",
                     values=("все", "готово", "проверка", "отказ", "ошибка")).pack(side="left", padx=(6, 14))
        self.status_filter.trace_add("write", lambda *_: self.filter_rows())
        self.history_var = tk.BooleanVar(value=False)
        self.history_pill = T.Pill(tools, "🕓 История: выкл", command=self._toggle_history,
                                   style="ghost", bg=T.BG_TOP)
        self.history_pill.pack(side="left", padx=(6, 0))
        self.view_mode = tk.StringVar(value="имя")
        tk.Label(tools, text=" · показ:", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left")
        mode_box = ttk.Combobox(tools, textvariable=self.view_mode, width=9, state="readonly",
                                values=("имя", "имя+путь", "путь"))
        mode_box.pack(side="left", padx=(2, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.render_rows())
        bar2 = tk.Frame(parent, bg=T.BG_TOP)
        bar2.pack(fill="x", pady=(4, 0))
        T.Pill(bar2, "🔁 Переразобрать выбранное", command=self.requeue_selected, style="ghost").pack(side="left")
        T.Pill(bar2, "🔬 Перепроверить глубже", command=self.recheck_selected, style="ghost").pack(side="left", padx=6)
        T.Pill(bar2, "🩺 Проверить странное", command=self.audit_anomalous, style="good").pack(side="left", padx=6)
        T.Pill(bar2, "📦 Папка целиком", command=self.run_folder_pass, style="accent").pack(side="left", padx=6)
        T.Pill(bar2, "🧠 Уроки", command=self.distill_lessons, style="quiet").pack(side="left", padx=6)
        T.Pill(bar2, "✔ Не трогать", command=self.keep_selected, style="quiet").pack(side="left", padx=6)
        T.Pill(bar2, "🧪 Пробный перенос", command=self.show_dry_run, style="ghost").pack(side="left", padx=6)
        T.Pill(bar2, "📦 Применить", command=self.apply_marked, style="accent").pack(side="left", padx=6)

        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True, pady=(4, 10))
        holder = card.inner
        holder.pack(fill="both")
        columns = ("apply", "status", "conf_name", "conf_move", "current", "new", "target", "op")
        widths = {"apply": 44, "status": 96, "conf_name": 58, "conf_move": 58,
                  "current": 420, "new": 340, "target": 340, "op": 180}
        heads = {"apply": "✓", "status": "Статус", "conf_name": "Имя", "conf_move": "Папка",
                 "current": "Было (текущий путь)",
                 "new": "Станет", "target": "Куда", "op": "Действие"}
        self.tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode="extended")
        for col in columns:
            self.tree.heading(col, text=heads[col], command=lambda c=col: self.sort_rows(c))
            self.tree.column(col, width=widths[col], minwidth=44, stretch=col in {"current", "new", "target"})
        ybar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        for tag, color in (("ready", T.GOOD), ("review", T.WARN), ("error", T.BAD), ("done", T.MUTED)):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<Double-1>", lambda _e: self.view_selected())
        self.tree.bind("<Return>", lambda _e: self.view_selected())
        self.tree.bind("<space>", lambda _e: self.toggle_apply())
        self.tree.bind("<Control-a>", lambda _e: self.select_all_rows())
        self.tree.bind("<Control-A>", lambda _e: self.select_all_rows())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())

        detail = T.Card(parent, padding=10, tone=T.CARD_SOFT)
        detail.pack(fill="x")
        detail.inner.pack(fill="x")
        tk.Label(detail.inner, text="Полные данные строки — без обрезки", bg=T.CARD_SOFT, fg=T.MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.details_box = tk.Text(detail.inner, height=8, wrap="word", bg=T.CARD, relief="flat",
                                   font=("Segoe UI", 10), padx=10, pady=8, fg=T.INK)
        self.details_box.pack(fill="x")
        self.details_box.configure(state="disabled")

    # --------------------------------------------------------------- структура
    def _build_tree(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text="Глубина", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left")
        self.depth_var = tk.IntVar(value=int(self.config["folders"].get("depth", 5)))
        ttk.Spinbox(bar, from_=1, to=9, width=4, textvariable=self.depth_var,
                    command=self.load_structure).pack(side="left", padx=6)
        tk.Label(bar, bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10), wraplength=520, justify="left",
                 text="Глубина = сколько ярусов раскрывать. Модель видит карту системы до настроенной "
                      "глубины, кроме скрытых ветвей («Разобрать»).").pack(side="left", padx=8)

        actions = tk.Frame(parent, bg=T.BG_TOP)
        actions.pack(fill="x", pady=(0, 8))
        T.Pill(actions, "📑 Аудит структуры", command=lambda: self._open_name("structure_audit.md"),
               style="accent").pack(side="left")
        T.Pill(actions, "📂 Папки", command=self.open_folders_window, style="good").pack(side="left", padx=6)
        T.Pill(actions, "📄 План папок", command=self.make_plan, style="ghost").pack(side="left")
        T.Pill(actions, "Создать папки из плана", command=self.create_plan, style="good").pack(side="left")
        self.plan_note = tk.Label(actions, bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10),
                                  wraplength=520, justify="left",
                                  text="план лежит в structure_plan.txt — правьте его перед созданием")
        self.plan_note.pack(side="left", padx=10)

        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        holder = card.inner
        holder.pack(fill="both")
        self.folder_tree = ttk.Treeview(holder, columns=("files",), show="tree headings")
        self.folder_tree.heading("#0", text="Папка")
        self.folder_tree.heading("files", text="Файлов в ветке")
        self.folder_tree.column("#0", width=420, minwidth=180, stretch=True)
        self.folder_tree.column("files", width=120, minwidth=90, anchor="e", stretch=False)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=scroll.set)
        self.folder_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------- структура: план папок
    def make_plan(self) -> None:
        """Выгрузить каркас в редактируемый файл плана и показать его."""
        from core import structure

        years = simpledialog.askstring(
            "Годы для подпапок",
            "Через пробел — какие годы создать в разделах-периодах (пусто = только разделы):",
            initialvalue="2024 2025 2026", parent=self)
        rows = structure.concrete_plan(self.config,
                                       years=tuple((years or "").split()))
        target = structure.write_plan(self.config, rows)
        self.plan_note.configure(text=f"план: {target.name}, строк {len(rows)} — отредактируйте и жмите «Создать»")
        open_path(target)
        self.append_log(f"план папок записан: {target}", "note")

    def create_plan(self) -> None:
        """Создать папки из плана. Только mkdir: существующие не трогаются, удалений нет."""
        from core import structure

        paths = structure.read_plan(self.config)
        if not paths:
            messagebox.showinfo("План", "Файл structure_plan.txt пуст — сначала нажмите «План папок».")
            return
        if not messagebox.askyesno("Создание папок",
                                   f"Будет создано до {len(paths)} папок (пустые, по плану). "
                                   "Ни один файл не перемещается и не удаляется. Продолжить?"):
            return
        if simpledialog.askstring("Подтверждение", "Введите CREATE:", parent=self) != "CREATE":
            return
        self.run_worker("Создание папок", lambda: structure.create_paths(
            self.config, paths, apply=True, log=self.log_event))

        # ------------------------------------------------------------------ промпт
    def _build_prompt(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text="Файл", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left")
        self.templates = templates_info(self.config["app_dir"])
        self.prompt_choice = tk.StringVar(value=self.templates[0][1])
        ttk.Combobox(bar, textvariable=self.prompt_choice, state="readonly", width=26,
                     values=[name for _, name, _ in self.templates]).pack(side="left", padx=6)
        self.prompt_choice.trace_add("write", lambda *_: self.load_prompt_editor())
        T.Pill(bar, "💾 Сохранить", command=self.save_prompt, style="accent").pack(side="left", padx=4)
        T.Pill(bar, "Образец", command=self.restore_prompt, style="ghost").pack(side="left")
        T.Pill(bar, "Открыть в редакторе", command=self.open_prompt_file, style="ghost").pack(side="left", padx=4)
        T.Pill(bar, "🔎 Проверить промпт", command=self.check_prompt, style="good").pack(side="left", padx=4)
        T.Pill(bar, "Показать запрос строки", command=self.preview_prompt, style="quiet").pack(side="right")
        self.prompt_info = tk.Label(bar, text="", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10))
        self.prompt_info.pack(side="right", padx=10)
        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        card.inner.pack(fill="both")
        self.prompt_text = tk.Text(card.inner, wrap="word", font=("Cascadia Mono", 10), bg=T.CARD,
                                   relief="flat", padx=12, pady=10, fg=T.INK, insertbackground=T.INK)
        self.prompt_text.pack(fill="both", expand=True)
        tk.Label(parent, bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10), wraplength=1280, justify="left",
                 text="Подстановки {roots}, {doc_types}, {folders_block}, {mode_line} программа заполняет сама. "
                      "Изменения действуют со следующей партии.").pack(anchor="w", pady=(6, 0))

    # ----------------------------------------------------------------- корзина
    def _build_trash(self, parent) -> None:
        intro = self._card(parent)
        tk.Label(intro, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 11), wraplength=1180, justify="left",
                 anchor="w", text=(
                     "🗑 Корзина отдела. Программа не удаляет файлы. Она собирает кандидатов — пустые, битые, "
                     "лишние копии и то, что модель сочла ненужным, — а по вашему решению переносит их в "
                     "отдельную папку. Удаляете там вы сами, вручную. Перенос пишется в журнал и откатывается.")
                 ).pack(anchor="w")
        self.trash_path_label = tk.Label(intro, bg=T.CARD, fg=T.INK, font=("Segoe UI Semibold", 10), anchor="w")
        self.trash_path_label.pack(anchor="w", pady=(4, 0))

        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(0, 8))
        T.Pill(bar, "🔄 Обновить список", command=self.load_trash, style="accent").pack(side="left")
        T.Pill(bar, "Отметить выбранное", command=lambda: self.trash_mark(True), style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "Снять отметку", command=lambda: self.trash_mark(False), style="ghost").pack(side="left")
        T.Pill(bar, "☑ Выделить все", command=self.trash_select_all, style="quiet").pack(side="left", padx=6)
        T.Pill(bar, "Убрать выделение", command=self.trash_clear_selection, style="ghost").pack(side="left")
        T.Pill(bar, "Перенести в корзину", command=self.move_trash, style="danger").pack(side="left", padx=6)
        T.Pill(bar, "📂 Показать в папке", command=self.reveal_trash, style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "Открыть файл", command=self.open_trash_file, style="ghost").pack(side="left")
        T.Pill(bar, "Открыть CSV", command=lambda: self._open_name("trash_candidates.csv"), style="ghost").pack(side="right")

        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        holder = card.inner
        holder.pack(fill="both")
        self.trash_tree = ttk.Treeview(holder, columns=("mark", "path", "size", "reason", "kind"), show="headings")
        self._trash_sort, self._trash_reverse = "", False
        for col, head, width, stretch in (("mark", "✓", 44, False), ("path", "Файл (полный путь)", 520, True),
                                          ("size", "Размер", 96, False), ("kind", "Признак", 110, False),
                                          ("reason", "Почему предлагаю убрать", 380, True)):
            self.trash_tree.heading(col, text=head, command=lambda c=col: self.sort_trash(c))
            self.trash_tree.column(col, width=width, minwidth=44, stretch=stretch)
        self.trash_tree.bind("<Control-a>", lambda _e: self.trash_select_all())
        self.trash_tree.bind("<Control-A>", lambda _e: self.trash_select_all())
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.trash_tree.yview)
        self.trash_tree.configure(yscrollcommand=scroll.set)
        self.trash_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.trash_tree.bind("<space>", lambda _e: self.trash_toggle())
        self.trash_tree.bind("<Return>", lambda _e: self.reveal_trash())
        self.trash_tree.bind("<Double-1>", self._trash_double, add="+")
        self.trash_tree.bind("<Button-1>", self._trash_click, add="+")
        self.trash_tree.bind("<<TreeviewSelect>>", lambda _e: self.show_trash_path())
        self._trash_last_iid = ""

        hint = T.Card(parent, padding=10, tone=T.CARD_SOFT)
        hint.pack(fill="x", pady=(8, 0))
        hint.inner.pack(fill="x")
        self.trash_path_box = tk.Label(hint.inner, text="Клик по строке — здесь появится полный путь к файлу.",
                                       bg=T.CARD_SOFT, fg=T.INK, font=("Segoe UI", 10), anchor="w",
                                       justify="left", wraplength=900)
        self.trash_path_box.pack(fill="x")

    # ----------------------------------------------------------------- плагины
    def _build_plugins(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(0, 8))
        T.Pill(bar, "🔄 Проверить", command=self.load_plugins, style="accent").pack(side="left")
        T.Pill(bar, "Проверить выбранное", command=self.verify_plugin, style="good").pack(side="left", padx=6)
        T.Pill(bar, "Установить выбранное", command=self.install_plugin, style="ghost").pack(side="left")
        T.Pill(bar, "Включить / выключить", command=self.toggle_plugin, style="ghost").pack(side="left", padx=6)
        tk.Label(bar, bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10), wraplength=520,
                 text="pip ставит библиотеки; внешние программы (Tesseract, ffmpeg) проверяем отдельно — "
                      "кнопкой «Проверить выбранное»").pack(side="left", padx=10)
        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        holder = card.inner
        holder.pack(fill="both")
        self.plugin_tree = ttk.Treeview(holder, columns=("title", "state", "enabled", "why", "how"), show="headings")
        for col, head, width in (("title", "Возможность", 240), ("state", "Статус", 260),
                                 ("enabled", "Включено", 90), ("why", "Что даёт", 420), ("how", "Как поставить", 280)):
            self.plugin_tree.heading(col, text=head)
            self.plugin_tree.column(col, width=width, minwidth=60)
        self.plugin_tree.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", T.GOOD), ("miss", T.WARN)):
            self.plugin_tree.tag_configure(tag, foreground=color)
        logcard = T.Card(parent, padding=8, tone=T.CARD_SOFT)
        logcard.pack(fill="x", pady=(10, 0))
        logcard.inner.pack(fill="x")
        self.plugin_log = tk.Text(logcard.inner, height=7, wrap="word", bg="#101828", fg="#e4e7ec",
                                  font=("Cascadia Mono", 10), relief="flat", padx=10, pady=8)
        self.plugin_log.pack(fill="x")

    # -------------------------------------------------------------- инструкция
    def _build_guide(self, parent) -> None:
        card = T.Card(parent, padding=12)
        card.pack(fill="both", expand=True)
        card.inner.pack(fill="both")
        box = tk.Text(card.inner, wrap="word", font=("Segoe UI", 11), bg=T.CARD, relief="flat",
                      padx=14, pady=10, fg=T.INK)
        box.insert("1.0", GUIDE_TEXT)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)
        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(10, 0))
        T.Pill(bar, "Дубликаты", command=self.start_duplicates, style="ghost").pack(side="left")
        T.Pill(bar, "Похожие фото", command=self.start_photo_dupes, style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "Битые файлы", command=self.run_broken, style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "Старые форматы: список", command=lambda: self.run_convert(False), style="ghost").pack(side="left")
        T.Pill(bar, "Старые: конвертировать", command=lambda: self.run_convert(True), style="good").pack(side="left", padx=6)
        T.Pill(bar, "Имена: предложения", command=lambda: self.run_repair(False), style="ghost").pack(side="left")
        T.Pill(bar, "Имена: применить", command=lambda: self.run_repair(True), style="danger").pack(side="left", padx=6)
        T.Pill(bar, "Оригиналы после конвертации", command=self.sweep_converted,
               style="ghost").pack(side="left", padx=6)
        tk.Label(bar, bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 9), wraplength=460, justify="left",
                 text="кнопка ищет .doc/.rtf/.odt, рядом с которыми уже есть .docx: список ляжет "
                      "в «Корзину» — оттуда переносите по своему решению").pack(side="left", padx=6)

    # --------------------------------------------------------------- настройки
    def _build_settings(self, parent) -> None:
        llm, folders = self.config["llm"], self.config["folders"]
        roots_dedup: list[str] = []
        for cand in [str(self.config["mo_root"])] + [str(c) for c in self.config.get("root_candidates", [])]:
            if cand.strip() and cand.strip().casefold() not in {x.casefold() for x in roots_dedup}:
                roots_dedup.append(cand.strip())
        self.svars = {
            "scan_root": tk.StringVar(value=str(self.config["scan_root"])),
            "roots": tk.StringVar(value="; ".join(roots_dedup)),
            "auto_pick": tk.BooleanVar(value=bool(self.config.get("auto_pick_root", True))),
            "output_dir": tk.StringVar(value=str(self.config["output_path"])),
            "quarantine": tk.StringVar(value=str(self.config.get("quarantine_dir", ""))),
            "model": tk.StringVar(value=str(llm["model_text"])),
            "vision_model": tk.StringVar(value=str(llm["model_vision"])),
            "base_url": tk.StringVar(value=str(llm.get("base_url", ""))),
            "num_ctx": tk.IntVar(value=int(llm["num_ctx"])),
            "vision_num_ctx": tk.IntVar(value=int(llm.get("vision_num_ctx", 16384))),
            "content_chars": tk.IntVar(value=int(llm["content_chars"])),
            "budget_chars": tk.IntVar(value=int(folders["budget_chars"])),
            "max_paths": tk.IntVar(value=int(folders["max_paths"])),
            "depth": tk.IntVar(value=int(folders["depth"])),
            "hierarchical": tk.BooleanVar(value=bool(folders.get("hierarchical", False))),
            "confidence": tk.DoubleVar(value=float(self.config["thresholds"]["min_confidence"])),
            "conf_name": tk.DoubleVar(value=float(self.config["thresholds"].get("min_name_confidence", self.config["thresholds"]["min_confidence"]))),
            "conf_move": tk.DoubleVar(value=float(self.config["thresholds"].get("min_move_confidence", self.config["thresholds"]["min_confidence"]))),
            "new_folder_cap": tk.DoubleVar(value=float(self.config["thresholds"].get("new_folder_max_confidence", 0.8))),
            "cache": tk.StringVar(value=str(llm["cache_policy"])),
            "ui_lang": tk.StringVar(value=str(self.config.get("ui_lang", "ru"))),
            "embed_model": tk.StringVar(value=str(llm.get("embed_model", ""))),
            "rules_first": tk.BooleanVar(value=bool(self.config.get("queue", {}).get("rules_first", True))),
            "hide_prefixes": tk.StringVar(value="; ".join(folders.get("hide_prefixes", []))),
            "vision": tk.BooleanVar(value=bool(llm.get("vision", True))),
            "ocr": tk.BooleanVar(value=bool(self.config.get("plugins", {}).get("ocr"))),
            "tesseract": tk.StringVar(value=str(self.config.get("plugins", {}).get("tesseract_cmd", ""))),
            "media_info": tk.BooleanVar(value=bool(self.config.get("plugins", {}).get("media_info"))),
            "doc_convert": tk.BooleanVar(value=bool(self.config.get("plugins", {}).get("doc_convert"))),
            "quarantine_on": tk.BooleanVar(value=bool(self.config.get("plugins", {}).get("quarantine", True))),
        }
        top = tk.Frame(parent, bg=T.BG_TOP)
        top.pack(fill="x", pady=(0, 8))
        T.Pill(top, "💾 Сохранить настройки", command=self.save_settings, style="accent").pack(side="left")
        T.Pill(top, "Диагностика", command=self.run_doctor, style="ghost").pack(side="left", padx=6)
        tk.Label(top, bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10), wraplength=700, justify="left",
                 text="страница прокручивается колесом мыши; всё сохранённое действует со следующей партии"
                 ).pack(side="left", padx=10)

        warn = self._card(parent)
        tk.Label(warn, bg=T.CARD, fg=T.WARN, font=("Segoe UI Semibold", 11), wraplength=1240, justify="left",
                 text=("⚠ Удаление. Программа сама файлы не стирает: «в корзину» — это перенос в отдельную "
                       "папку (по умолчанию «99_Корзина на проверку»), а уже оттуда вы удаляете вручную. "
                       "Перенос пишется в журнал и откатывается.")).pack(anchor="w")

        block = self._card(parent, "📁 Пути")
        self._field_with_browse(block, "Корень сканирования", self.svars["scan_root"])
        self._entry_field(block, "Варианты корня отдела (первый — основной, остальные через «;»)",
                          self.svars["roots"],
                          "один и тот же Google Drive на разных компьютерах монтируется под разными "
                          "буквами — допишите сюда путь отдела с каждого компьютера (например "
                          "D:\\Общие диски\\Молодежный отдел)")
        tk.Checkbutton(block, text="автовыбор первого существующего корня из списка (для разных ПК)",
                       bg=T.CARD, fg=T.INK, activebackground=T.CARD, selectcolor=T.CARD_SOFT,
                       anchor="w", font=("Segoe UI", 10),
                       variable=self.svars["auto_pick"]).pack(fill="x", pady=(2, 4))
        self._field_with_browse(block, "Папка результатов", self.svars["output_dir"])
        self._field_with_browse(block, "Папка корзины", self.svars["quarantine"])

        block = self._card(parent, "🤖 Модель и контекст")
        self._entry_field(parent=block, label="Адрес Ollama (свой ПК или другой в сети)",
                          variable=self.svars["base_url"],
                          note="по умолчанию http://127.0.0.1:11434. Нет видеокарты здесь — "
                               "впишите http://адрес-компьютера-с-картой:11434, и модель будет "
                               "думать на нём (там должен быть запущен Ollama с OLLAMA_HOST=0.0.0.0)")
        self._combo_field(block, "Текстовая модель", self.svars["model"], self._model_names())
        self._combo_field(block, "Модель для зрения", self.svars["vision_model"], self._model_names())
        self._spin_field(block, "num_ctx", self.svars["num_ctx"], 4096, 65536, 4096,
                         "8192 бережёт видеопамять; 16384 — карта без длинной инструкции; "
                         "32768 — полная инструкция + полная карта + текст файла")
        self._spin_field(block, "num_ctx для зрения", self.svars["vision_num_ctx"], 4096, 65536, 4096,
                         "окно только для разбора картинок и PDF-сканов (vision-модель); "
                         "держите меньше основного, чтобы не переполнить видеопамять")
        self._spin_field(block, "Символов текста файла", self.svars["content_chars"], 800, 20000, 500)
        self._entry_field(block, "Путь к tesseract.exe (для OCR)", self.svars["tesseract"],
                          "пусто — искать самому: PATH, Program Files\\Tesseract-OCR. "
                          "Без движка pip ничего не распознаёт")
        self.context_line = tk.Label(block, text="", bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10),
                                     wraplength=1000, justify="left", anchor="w")
        self.context_line.pack(fill="x", pady=(6, 0))
        self._update_context_line()
        self._combo_field(block, "Очистка кэша перед файлом", self.svars["cache"], ("unload", "nonce", "none"),
                          readonly=True, note="unload — полный сброс: контекст чист, но каждый файл дольше")

        block = self._card(parent, "🗺 Карта папок для модели")
        self._spin_field(block, "Бюджет карты, символов", self.svars["budget_chars"], 3000, 40000, 1000)
        self._spin_field(block, "Максимум путей", self.svars["max_paths"], 100, 2000, 50)
        self._spin_field(block, "Глубина карты (ярусов)", self.svars["depth"], 1, 9, 1,
                         "5 ≈ вся карта системы без «Разобрать»")
        self._entry_field(block, "Скрыть ветви от модели", self.svars["hide_prefixes"],
                          "через точку с запятой; зону разбора держать скрытой — её структура ошибочна")
        tk.Checkbutton(block, text="двухпроходный разбор: сначала раздел → затем точная папка внутри него",
                       bg=T.CARD, fg=T.INK, activebackground=T.CARD, selectcolor=T.CARD_SOFT,
                       anchor="w", font=("Segoe UI", 10),
                       variable=self.svars["hierarchical"]).pack(fill="x", pady=(6, 0))
        tk.Label(block, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9), wraplength=760, justify="left",
                 text=("так модель видит ВСЮ глубину вложенности: в первом запросе — только скелет "
                       "разделов, во втором — полное поддерево выбранного раздела. Цена: ~2 запроса "
                       "на файл (примерно вдвое дольше, качество раскладки выше)")) .pack(anchor="w")

        block = self._card(parent, "⚖️ Решения и возможности")
        from ui import i18n as _I
        self._combo_field(block, "Язык интерфейса (после перезапуска)", self.svars["ui_lang"],
                          tuple(_I.LANGS.values()), readonly=True,
                          note="ключи словаря — русские строки; дополнять переводы можно в ui/i18n.py")
        tk.Checkbutton(block, text="правила раньше модели: однозначные случаи решаются без запроса (быстрее и стабильнее)",
                       bg=T.CARD, fg=T.INK, activebackground=T.CARD, selectcolor=T.CARD_SOFT,
                       anchor="w", font=("Segoe UI", 10),
                       variable=self.svars["rules_first"]).pack(fill="x", pady=(4, 1))
        self._entry_field(block, "Модель эмбеддингов Ollama (поиск папок по смыслу; пусто = выкл)",
                          self.svars["embed_model"],
                          "лучше всего bge-m3 (мультиязычная, сильна на русском): `ollama pull bge-m3`; "
                          "векторы папок кэшируются рядом с программой")
        self._spin_field(block, "Порог автодоверия", self.svars["confidence"], 0.5, 1.0, 0.05)
        self._spin_field(block, "Порог уверенности в имени", self.svars["conf_name"], 0.5, 1.0, 0.05)
        self._spin_field(block, "Порог уверенности в переносе", self.svars["conf_move"], 0.5, 1.0, 0.05)
        self._spin_field(block, "Потолок при новой папке", self.svars["new_folder_cap"], 0.3, 1.0, 0.05,
                         "если папки нет в дереве — уверенность режется, строка на проверку")
        for key, label in (("vision", "зрение модели для картинок и PDF-сканов"),
                           ("ocr", "распознавание текста (OCR) перед отправкой в модель"),
                           ("media_info", "метаданные фото, аудио и видео"),
                           ("doc_convert", "разрешить конвертацию старых форматов"),
                           ("quarantine_on", "собирать кандидатов в корзину и разрешить перенос")):
            tk.Checkbutton(block, text=label, bg=T.CARD, fg=T.INK, activebackground=T.CARD,
                           selectcolor=T.CARD_SOFT, anchor="w", font=("Segoe UI", 10),
                           variable=self.svars[key]).pack(fill="x", pady=1)

        buttons = tk.Frame(parent, bg=T.BG_TOP)
        buttons.pack(fill="x", pady=(4, 10))
        T.Pill(buttons, "💾 Сохранить настройки", command=self.save_settings, style="accent").pack(side="left")
        T.Pill(buttons, "Диагностика", command=self.run_doctor, style="ghost").pack(side="left", padx=6)
        T.Pill(buttons, "Открыть config.json", command=lambda: open_path(self.config["config_path"]),
               style="ghost").pack(side="left")
        for key in ("num_ctx", "content_chars", "budget_chars"):
            self.svars[key].trace_add("write", self._update_context_line)

    def _entry_field(self, parent, label: str, variable, note: str = "") -> None:
        from ui import i18n as _I
        label = _I.tr(label)
        tk.Label(parent, text=label, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10)).pack(anchor="w", pady=(8, 2))
        tk.Entry(parent, textvariable=variable, bg=T.CARD_SOFT, fg=T.INK, relief="flat",
                 highlightthickness=1, highlightbackground=T.BORDER, highlightcolor=T.ACCENT,
                 insertbackground=T.INK, font=("Segoe UI", 11)).pack(fill="x", ipady=5)
        if note:
            tk.Label(parent, text=note, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9),
                     wraplength=760, justify="left").pack(anchor="w")

    def _field_with_browse(self, parent, label: str, variable) -> None:
        from ui import i18n as _I
        label = _I.tr(label)
        row = tk.Frame(parent, bg=T.CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=T.CARD, fg=T.MUTED, width=26, anchor="w",
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Entry(row, textvariable=variable, bg=T.CARD_SOFT, fg=T.INK, relief="flat",
                 highlightthickness=1, highlightbackground=T.BORDER, highlightcolor=T.ACCENT,
                 insertbackground=T.INK, font=("Segoe UI", 11)).pack(side="left", fill="x", expand=True, ipady=5)
        T.Pill(row, "…", command=lambda: self.pick(variable), style="ghost", width=48).pack(side="left", padx=(6, 0))

    def _spin_field(self, parent, label: str, variable, low, high, step, note: str = "") -> None:
        from ui import i18n as _I
        label = _I.tr(label)
        row = tk.Frame(parent, bg=T.CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=T.CARD, fg=T.MUTED, width=26, anchor="w",
                 font=("Segoe UI", 10)).pack(side="left")
        ttk.Spinbox(row, from_=low, to=high, increment=step, textvariable=variable, width=12).pack(side="left")
        if note:
            tk.Label(row, text="  " + note, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9),
                     wraplength=560, justify="left").pack(side="left", padx=8)

    def _combo_field(self, parent, label: str, variable, values, readonly: bool = False, note: str = "") -> None:
        from ui import i18n as _I
        label = _I.tr(label)
        row = tk.Frame(parent, bg=T.CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=T.CARD, fg=T.MUTED, width=26, anchor="w",
                 font=("Segoe UI", 10)).pack(side="left")
        ttk.Combobox(row, textvariable=variable, values=list(values), width=28,
                     state="readonly" if readonly else "normal").pack(side="left")
        if note:
            tk.Label(row, text="  " + note, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9),
                     wraplength=560, justify="left").pack(side="left", padx=8)

    # ------------------------------------------------------------------ журнал
    def _build_log(self, parent) -> None:
        bar = tk.Frame(parent, bg=T.BG_TOP)
        bar.pack(fill="x", pady=(0, 8))
        T.Pill(bar, "Очистить экран", command=lambda: self.log_text.delete("1.0", "end"), style="ghost").pack(side="left")
        T.Pill(bar, "Журнал операций", command=lambda: self._open_name("operations_log.csv"), style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "История переносов", command=lambda: self._open_name(mapio.APPLIED_NAME), style="ghost").pack(side="left")
        T.Pill(bar, "Проблемные файлы", command=lambda: self._open_name("broken_files.csv"), style="ghost").pack(side="left", padx=6)
        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        card.inner.pack(fill="both")
        self.log_text = tk.Text(card.inner, wrap="word", bg="#101828", fg="#e4e7ec", insertbackground="white",
                                font=("Cascadia Mono", 10), relief="flat", padx=12, pady=10)
        self.log_text.pack(fill="both", expand=True)
        for tag, color in (("error", "#fda29b"), ("ok", "#86efac"), ("note", "#93c5fd")):
            self.log_text.tag_configure(tag, foreground=color)

    # ------------------------------------------------------------------- общее
    def _model_names(self) -> tuple[str, ...]:
        from core.llm import OllamaClient

        client = OllamaClient(base_url=self.config["llm"]["base_url"], model=self.config["llm"]["model_text"])
        names = client.list_models() if client.is_alive() else []
        return tuple(sorted(set(names) | {self.config["llm"]["model_text"], self.config["llm"]["model_vision"]}))

    def _open_name(self, name: str) -> None:
        path = self.config["output_path"] / name
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo("Нет файла", f"{name} ещё не создан.")

    def run_worker(self, name: str, func) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Занято", "Дождитесь конца текущей операции или нажмите «Стоп».")
            return
        self.status_var.set(name)
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(10)

        def target() -> None:
            try:
                self.events.put(("done", name, func()))
            except Exception as exc:
                self.events.put(("error", name, exc, traceback.format_exc()))

        self.worker = threading.Thread(target=target, daemon=True, name=name)
        self.worker.start()

    def _drain(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.append_log(event[1])
                elif kind == "progress":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(1, event[2]), value=event[1])
                    self.status_var.set(f"{event[3] if len(event) > 3 else 'Ход'}: {event[1]}/{event[2]}")
                elif kind == "record":
                    row = event[1]
                    self.append_log(f"{row.get('Статус')} · {row.get('Текущий путь')} → "
                                    f"{row.get('Целевой путь') or '—'}/{row.get('Новое имя')}")
                elif kind == "ollama":
                    alive, models = event[1], event[2]
                    model = self.config["llm"]["model_text"]
                    ready = alive and model in models
                    self.set_dot(T.GOOD if ready else (T.WARN if alive else T.BAD))
                    self.ollama_label.configure(text=(f"Ollama · {model} готова" if ready else
                                                      (f"Ollama · нет модели {model}" if alive else "Ollama · не отвечает")))
                elif kind == "snapshot":
                    self._apply_snapshot(event[1], event[2], event[3])
                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100, value=100)
                    result = event[2]
                    text = (", ".join(f"{k}: {v}" for k, v in result.items())
                            if isinstance(result, dict) else (str(result) if result else ""))
                    self.status_var.set(f"Готово — {text}" if text else "Готово")
                    self.append_log(f"Готово — {text}" if text else "Готово", "ok")
                    self.refresh_all()
                    if event[1] == "Перенос":
                        self.after(400, self._check_intake_empty)
                elif kind == "error":
                    self.progress.stop()
                    self.status_var.set("Ошибка")
                    self.append_log(event[3], "error")
                    messagebox.showerror("Ошибка", f"{type(event[2]).__name__}: {event[2]}")
        except queue.Empty:
            pass
        self.after(90, self._drain)

    def log_event(self, text: str) -> None:
        self.events.put(("log", text))

    def append_log(self, text: str, tag: str = "") -> None:
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n", tag)
        self.log_text.see("end")

    # ---------------------------------------------------------------- действия
    def _toggle_history(self) -> None:
        self.history_var.set(not self.history_var.get())
        on = self.history_var.get()
        self.history_pill.set_style("accent" if on else "ghost")
        self.history_pill.configure_text("🕓 История: вкл" if on else "🕓 История: выкл")
        self.load_map()

    def refresh_all(self) -> None:
        """Лёгкое — сразу, тяжёлое (снимок с сетевого диска) — в фоне: окно не виснет."""
        self.load_map()
        self.load_structure()
        self.load_plugins()
        self.load_prompt_editor()
        self.load_trash(silent=True)
        self.check_ollama()
        if hasattr(self, "mode"):
            mode = str(self.config["queue"].get("mode", "завалы"))
            index = MODE_KEYS.index(mode) if mode in MODE_KEYS else 0
            self.mode.select(index, slide=False)
            self._mode_note()
        if getattr(self, "_snapshot_loading", False):
            return
        self._snapshot_loading = True

        def work() -> None:
            snapshot = None
            index_obj = None
            error = None
            try:
                snapshot = scanner.load_snapshot(self.config)
                if snapshot:
                    index_obj = FolderIndex.from_snapshot(
                        snapshot,
                        prune=[*sorted(self.config["excluded_dirs"]), self.config["output_path"].name],
                        exclude=self.config["folders"].get("hide_prefixes", []),
                    )
            except Exception as exc:
                error = exc
            self.events.put(("snapshot", snapshot, index_obj, error))

        threading.Thread(target=work, daemon=True, name="snapshot").start()

    def _apply_snapshot(self, snapshot, index_obj, error) -> None:
        self._snapshot_loading = False
        if error is not None:
            self.append_log(f"снимок не читается: {error}", "error")
            self.snapshot, self.index = None, None
        else:
            self.snapshot, self.index = snapshot, index_obj
        if not self.snapshot:
            for name in ("Файлов", "Папок", "К разбору"):
                self.cards[name].configure(text="—")
            self.home_info.configure(text="Снимка нет. Начните с «Обновить снимок».")
            return
        self.cards["Файлов"].configure(text=f"{len(self.snapshot['files']):,}".replace(",", " "))
        self.cards["Папок"].configure(text=f"{len(self.snapshot['folders']):,}".replace(",", " "))
        hidden = ", ".join(self.config["folders"].get("hide_prefixes", [])) or "нет"
        self.home_info.configure(text=f"Снимок от {self.snapshot.get('created', '')}. "
                                      f"Скрыто от модели: {hidden}. Папок в индексе: {self.index.folder_count}.")
        self.load_structure()
        self.load_map()

    def load_snapshot(self) -> None:
        """Синхронная версия — нужна редким вызовам после сканирования."""
        self._apply_snapshot(*self._read_snapshot_pair())

    def _read_snapshot_pair(self):
        try:
            snapshot = scanner.load_snapshot(self.config)
        except Exception as exc:
            return None, None, exc
        if not snapshot:
            return None, None, None
        index_obj = FolderIndex.from_snapshot(
            snapshot,
            prune=[*sorted(self.config["excluded_dirs"]), self.config["output_path"].name],
            exclude=self.config["folders"].get("hide_prefixes", []),
        )
        return snapshot, index_obj, None

    def start_scan(self) -> None:
        if not messagebox.askokcancel("Снимок диска", "Прочитаю дерево и хеши. Файлы не меняются. Продолжить?"):
            return

        def work():
            snapshot = scanner.scan(self.config, progress=lambda n, c: self.events.put(("progress", c, c + 1, "Обход")))
            return {"файлов": len(snapshot["files"]), "папок": len(snapshot["folders"])}

        self.run_worker("Сканирование", work)

    def start_duplicates(self) -> None:
        self.run_worker("Дубликаты", lambda: scanner.duplicates_report(self.config))

    def start_photo_dupes(self) -> None:
        def work():
            from core import photodupes
            rows = photodupes.find_similar(self.config)
            path = photodupes.save_report(self.config, rows)
            return {"групп": len(rows), "отчёт": path.name}
        self.run_worker("Похожие фото", work)

    def start_analyze(self) -> None:
        if not self.snapshot or not self.index:
            messagebox.showwarning("Нет снимка", "Сначала обновите снимок диска.")
            return
        mode = str(self.config["queue"].get("mode", "завалы"))
        prefix = self.quick["prefix"].get().strip() if mode == "завалы" else ""
        filters = Filters(prefix=prefix, limit=max(1, int(self.quick["limit"].get())),
                          include_media=bool(self.quick["media"].get()),
                          order=str(self.config["queue"].get("order", "shallow")),
                          skip_done=not bool(self.quick["redo"].get()))
        self.analyzer = Analyzer(self.config, self.snapshot, self.index, log=self.log_event,
                                 on_record=lambda row: self.events.put(("record", row)),
                                 on_progress=lambda done, total: self.events.put(("progress", done, total, "Разбор")))
        self.analyzer.vision_enabled = bool(self.quick["vision"].get())
        if self.analyzer.vision_enabled and self.analyzer.vision_client is None:
            self.analyzer.vision_client = self.analyzer.client

        def work():
            records = self.analyzer.run(filters)
            rows = mapio.load_map(self.config["output_path"] / MAP_NAME)
            rows, added, updated, skipped = mapio.merge_suggestions(rows, records)
            mapio.save_map(self.config["output_path"] / MAP_NAME, rows)
            return {"разобрано": len(records), "строк в карте": added + updated, "правок не тронуто": skipped}

        self.run_worker(f"Разбор {filters.limit} файлов", work)

    def stop_work(self) -> None:
        if self.analyzer:
            self.analyzer.stop_flag.set()
            self.status_var.set("Останавливаюсь после текущего файла…")

    def pause_work(self) -> None:
        if not self.analyzer:
            return
        if self.analyzer.pause_flag.is_set():
            self.analyzer.pause_flag.clear()
            self.status_var.set("Продолжено")
        else:
            self.analyzer.pause_flag.set()
            self.status_var.set("Пауза после текущего файла")

    def show_dry_run(self) -> None:
        self.save_map(silent=True)
        plan = applier.build_plan(self.config, self.rows)
        lines, counters = applier.describe(plan, self.config)
        win = tk.Toplevel(self)
        win.title("Пробный перенос — файлы не изменяются")
        win.geometry("1060x660")
        win.configure(bg=T.CARD)
        tk.Label(win, bg=T.CARD, fg=T.INK, font=("Segoe UI Semibold", 12), padx=14, pady=12, justify="left",
                 text=(f"Файлов: {counters['всего']} · Объём: {fmt_size(counters['байт'])} · "
                       f"Новых папок: {counters['новых папок']} · Конфликтов имён: {counters['конфликтов']} · "
                       "удалений нет")).pack(fill="x")
        box = tk.Text(win, wrap="none", font=("Cascadia Mono", 10), padx=12, pady=10, bg=T.CARD, fg=T.INK)
        box.pack(fill="both", expand=True)
        box.insert("end", "\n".join(lines) if lines else "Нет отмеченных действий.")
        box.configure(state="disabled")

    def apply_marked(self) -> None:
        self.save_map(silent=True)
        plan = applier.build_plan(self.config, self.rows)
        if not plan:
            marked = [r for r in self.rows if (r.get("Применить") or "").strip().upper() == "TRUE"]
            if not marked:
                messagebox.showinfo("Нечего переносить",
                                    "Ни одна строка не помечена. Выделите строки и нажмите «Отметить» "
                                    "(или Пробел), затем «✅ Утвердить помеченные».")
            else:
                no_name = sum(1 for r in marked if not (r.get("Новое имя") or "").strip())
                missing = sum(1 for r in marked
                              if (r.get("Новое имя") or "").strip()
                              and not (self.config["scan_root"] / str(r.get("Текущий путь", "")).replace("\\", "/")).exists())
                same = len(marked) - no_name - missing
                messagebox.showinfo("Нечего переносить",
                                    f"Помечено строк: {len(marked)}, но переносить нечего:\n"
                                    f"  • нет нового имени — {no_name}\n"
                                    f"  • файла уже нет на месте (перенесён вручную) — {missing}\n"
                                    f"  • имя и папка совпадают с текущими (менять нечего) — {same}\n\n"
                                    "Заполните «Новое имя»/«Целевую папку» в просмотрщике или "
                                    "нажмите «🔬 Перепроверить глубже».")
            return
        _, counters = applier.describe(plan, self.config)
        if not messagebox.askyesno("Перенос",
                                   f"Будет ПЕРЕМЕЩЕНО {len(plan)} файл(ов), {fmt_size(counters['байт'])}.\n"
                                   f"Новых папок: {counters['новых папок']}, конфликтов: {counters['конфликтов']}.\n"
                                   "Удаления не будет. Продолжить?"):
            return
        if simpledialog.askstring("Подтверждение", "Введите APPLY:", parent=self) != "APPLY":
            return
        self.run_worker("Перенос", lambda: applier.apply_plan(
            self.config, self.rows, progress=lambda i, n, rel: self.events.put(("progress", i, n, "Перенос"))))

    def undo_last(self) -> None:
        if not messagebox.askyesno("Откат", "Вернуть файлы последней пачки на прежние места?"):
            return
        self.run_worker("Откат", lambda: applier.undo_last(
            self.config, progress=lambda i, n, rel: self.events.put(("progress", i, n, "Откат"))))

    # ------------------------------------------------------------ папки
    def open_folders_window(self) -> None:
        from ui.folders_window import FoldersWindow
        win = getattr(self, "_folders_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.refresh_all()
            return
        self._folders_win = FoldersWindow(self, self.config, log=self.log_event)

    def _check_intake_empty(self) -> None:
        """Если зона «Разобрать» опустела — мягко предложить убрать её оболочку."""
        from core import folders_ops
        root = Path(str(self.config["scan_root"]))
        empties: list[str] = []
        for pref in self.config.get("folders", {}).get("hide_prefixes", []):
            rel = str(pref).replace("\\", "/").strip("/")
            if not rel:
                continue
            path = root / rel
            if path.is_dir() and folders_ops.is_dir_empty(path):
                empties.append(rel)
        if not empties:
            return
        if messagebox.askyesno(
                "Зона разбора пуста",
                "В «" + "» и «".join(empties) + "» больше нет файлов.\n"
                "Убрать пустые папки в корзину Windows? (фоновой работе это не мешает;\n"
                "позже список посмотрите в окне «Структура → 📂 Папки»)"):
            def work() -> None:
                for rel in empties:
                    ok, msg = folders_ops.recycle_folder(self.config, rel)
                    self.events.put(("log", f"зона разбора «{rel}»: "
                                  + ("в корзину" if ok else f"пропуск ({msg})")))
            self.run_worker("Очистка зоны разбора", work)

    # -------------------------------------------------------------------- карта
    def load_map(self) -> None:
        self.showing_history = bool(self.history_var.get())
        name = mapio.APPLIED_NAME if self.showing_history else MAP_NAME
        self.rows = mapio.load_map(self.config["output_path"] / name)
        self.details = {}
        for record in mapio.load_records(self.config["output_path"] / SUGGESTIONS_NAME):
            self.details[str(record.get("Текущий путь", ""))] = record
        self.filter_rows()
        active = mapio.load_map(self.config["output_path"] / MAP_NAME)
        stats = mapio.stats(active)
        history = mapio.load_map(self.config["output_path"] / mapio.APPLIED_NAME)
        self.cards["На проверке"].configure(text=str(stats["проверка"]))
        self.cards["Отмечено"].configure(text=str(stats["отмечено"]))
        self.cards["В истории"].configure(text=str(len(history)))
        if self.snapshot and not self.showing_history:
            mapped = {(r.get("Текущий путь") or "").replace("\\", "/") for r in active}
            left = sum(1 for item in self.snapshot["files"]
                       if str(item.get("path", "")).replace("\\", "/") not in mapped)
            self.cards["К разбору"].configure(text=f"{left:,}".replace(",", " "))

    def filter_rows(self) -> None:
        query = self.search_var.get().casefold().strip()
        status = self.status_filter.get()
        rows = []
        for row in self.rows:
            if not self.showing_history and status != "все" and row.get("Статус") != status:
                continue
            if query and query not in " ".join(str(row.get(k, "")) for k in mapio.MAP_FIELDS).casefold():
                continue
            rows.append(row)
        self.filtered = rows
        self.render_rows()

    def render_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        mode = getattr(self, "view_mode", None)
        mode = mode.get() if mode else "имя"
        th = self.config.get("thresholds", {})
        base = float(th.get("min_confidence", 0.9))
        thr_name = float(th.get("min_name_confidence", base))
        thr_move = float(th.get("min_move_confidence", base))

        def mark(value, threshold):
            raw = "" if value in (None, "") else str(value)
            try:
                low = float(value) < threshold
            except (TypeError, ValueError):
                return raw
            return (f"⚠ {raw}" if low else raw)

        for index, row in enumerate(self.filtered):
            status = (row.get("Статус") or "").strip()
            tag = {"готово": "ready", "проверка": "review", "ошибка": "error",
                   "перенесено": "done", "отказ": "done", "без изменений": "done"}.get(status, "")
            name = row.get("Новое имя", "")
            target = row.get("Целевой путь", "")
            shown = {"имя": name,
                     "имя+путь": f"{target + '/' if target else ''}{name}",
                     "путь": target}.get(mode, name)
            self.tree.insert("", "end", iid=str(index), tags=(tag,), values=(
                "☑" if (row.get("Применить") or "").upper() == "TRUE" else "☐",
                status, mark(row.get("Уверенность имени", ""), thr_name),
                mark(row.get("Уверенность переноса", ""), thr_move),
                row.get("Текущий путь", ""),
                shown, row.get("Целевой путь", ""), row.get("Тип операции", ""),
            ))

    def selected_rows(self) -> list[dict]:
        return [self.filtered[int(iid)] for iid in self.tree.selection()
                if iid.isdigit() and int(iid) < len(self.filtered)]

    def set_apply(self, value: bool) -> None:
        rows = self.selected_rows()
        if value and any(row.get("Статус") == "проверка" for row in rows):
            if not messagebox.askyesno("Отметка проверочной строки",
                                       "Среди выбранных есть «проверка». Отметить и их?"):
                rows = [row for row in rows if row.get("Статус") != "проверка"]
        for row in rows:
            if row.get("Статус") != "перенесено":
                row["Применить"] = "TRUE" if value else "FALSE"
        self.render_rows()
        self.save_map(silent=True)

    def toggle_apply(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.set_apply(any((row.get("Применить") or "").upper() != "TRUE" for row in rows))

    def approve_marked(self) -> None:
        """Утвердить разом всё помеченное: «проверка» → «готово» с отметкой к переносу."""
        marked = [row for row in self.rows if (row.get("Применить") or "").upper() == "TRUE"
                  and row.get("Статус") != "перенесено"]
        waiting = [row for row in marked if row.get("Статус") == "проверка"]
        if not marked:
            messagebox.showinfo("Утверждение", "Сначала отметьте строки (пробел или кнопка «Отметить»).")
            return
        if not messagebox.askyesno("Утвердить помеченные",
                                   f"Помечено строк: {len(marked)}.\nИз них сейчас «проверка»: {len(waiting)} — "
                                   "они станут «готово».\n\nУтвердить всё разом?"):
            return
        for row in marked:
            row["Статус"] = "готово"
            row["Применить"] = "TRUE"
        self.save_map(silent=True)
        self.filter_rows()
        self.append_log(f"утверждено: {len(marked)} (снято с проверки: {len(waiting)})", "ok")

    def save_map(self, silent: bool = False) -> None:
        if self.showing_history:
            if not silent:
                messagebox.showinfo("История", "Файл истории только для чтения.")
            return
        mapio.save_map(self.config["output_path"] / MAP_NAME, self.rows)
        if not silent:
            self.status_var.set("Карта сохранена")

    def sort_rows(self, column: str) -> None:
        field = {"apply": "Применить", "status": "Статус", "conf_name": "Уверенность имени",
                 "conf_move": "Уверенность переноса", "current": "Текущий путь",
                 "new": "Новое имя", "target": "Целевой путь", "op": "Тип операции"}[column]
        reverse = self._sort_field == field and not self._sort_reverse
        self._sort_field, self._sort_reverse = field, reverse
        if column in {"conf_name", "conf_move"}:
            def _num(r):
                try:
                    return float(str(r.get(field, "")).strip().lstrip("⚠ ").strip() or -1)
                except ValueError:
                    return -1
            self.rows.sort(key=_num, reverse=reverse)
        else:
            self.rows.sort(key=lambda r: str(r.get(field, "")).casefold(), reverse=reverse)
        self.filter_rows()

    def show_details(self) -> None:
        rows = self.selected_rows()
        self.details_box.configure(state="normal")
        self.details_box.delete("1.0", "end")
        if not rows:
            self.details_box.insert("1.0", "Выберите строку — здесь появятся полные данные без обрезки.")
            return
        row = rows[0]
        extra = self.details.get(row.get("Текущий путь", ""), {})
        lines = [
            f"Где сейчас   : {row.get('Текущий путь')}",
            f"Новое имя    : {row.get('Новое имя')}",
            f"Куда         : {row.get('Целевой путь')}",
            f"Действие     : {row.get('Тип операции')} · {row.get('Статус')} · "
            f"имя {row.get('Уверенность имени')} · папка {row.get('Уверенность переноса')} · применить {row.get('Применить')}",
            f"Причина      : {row.get('Причина')}",
        ]
        if row.get("Путь ярлыка"):
            lines.append(f"Путь ярлыка  : {row.get('Путь ярлыка')}")
        if row.get("Комментарий"):
            lines.append(f"Комментарий  : {row.get('Комментарий')}")
        if extra:
            facts = " · ".join(f"{k[1:]}: {extra[k]}" for k in
                               ("_тип_документа", "_направление", "_дата", "_дата-события", "_номер",
                                "_тема", "_событие") if extra.get(k))
            if facts:
                lines.append("Признаки     : " + facts)
            lines.append(f"Читала модель: {extra.get('_вид_содержимого')} · {extra.get('_прочитано_символов')} симв. · "
                         f"{extra.get('_время_запроса')} с · {extra.get('_модель')}")
            if extra.get("_контекст"):
                lines.append("Контекст     : " + str(extra["_контекст"]))
            if extra.get("_несколько_документов"):
                lines.append("⚠ В файле несколько документов: " + str(extra["_несколько_документов"]))
            if extra.get("_флаги"):
                lines.append("Флаги        : " + str(extra["_флаги"]))
            if extra.get("_исправления"):
                lines.append("Исправлено   : " + str(extra["_исправления"]))
            if extra.get("_дубликаты"):
                lines.append("Совпадения   : " + str(extra["_дубликаты"]))
        self.details_box.insert("1.0", "\n".join(lines))

    def view_selected(self) -> None:
        rows = self.selected_rows()
        if not rows:
            messagebox.showinfo("Просмотр", "Выберите строку карты.")
            return
        if len(rows) > 1 and not messagebox.askyesno("Просмотр", f"Выбрано {len(rows)} строк. Открыть первую?"):
            return
        row = rows[0]

        def after_save(updated: dict) -> None:
            row.update(updated)
            if (updated.get("Статус") or "").strip() == "без изменений":
                mapio.archive_manual(self.config["output_path"], row)
                self.rows = [r for r in self.rows if r is not row]
                mapio.save_map(self.config["output_path"] / MAP_NAME, self.rows)
                self.load_map()          # перечитать активную карту и обновить счётчики
                self.show_details()
                return
            self.save_map(silent=True)
            self.filter_rows()
            self.show_details()

        open_viewer(self, self.config, row, on_save=after_save, on_apply=self._apply_one)

    def select_all_rows(self) -> None:
        """Выделить все строки текущего фильтра — дальше «Отметить» работает разом."""
        iids = list(self.tree.get_children())
        self.tree.selection_set(iids)
        if iids:
            self.tree.focus(iids[0])
        self.status_var.set(f"выделено строк: {len(iids)}")
        self.show_details()

    # ------------------------------------------- карта: переразбор, уроки, папка
    def requeue_selected(self) -> None:
        """Вернуть строки модели без нового скана диска: убрать из карты и отметок."""
        rows = self.selected_rows()
        if not rows:
            rejected = [r for r in self.rows if str(r.get("Статус", "")).strip() == "отказ"]
            if rejected and messagebox.askyesno(
                    "Переразбор",
                    f"Ничего не выделено. Вернуть в очередь все {len(rejected)} строк со статусом «отказ»?"):
                rows = rejected
            else:
                messagebox.showinfo("Переразбор", "Выделите строки в карте (или Ctrl+A — все).")
                return
        paths = {str(r.get("Текущий путь", "")).replace("\\", "/") for r in rows}
        if not messagebox.askyesno("Переразбор",
                                   f"Строк: {len(paths)}.\nОни уйдут из карты, и модель предложит им новые "
                                   "имя и папку заново (скан диска не нужен). Продолжить?"):
            return
        self.rows = [r for r in self.rows
                     if str(r.get("Текущий путь", "")).replace("\\", "/") not in paths]
        self._scrub_suggestions(paths)
        self.save_map(silent=True)
        self.filter_rows()
        self.append_log(f"вернуто в очередь строк: {len(paths)} — запустите партию на странице «Обзор»", "ok")

    def _scrub_suggestions(self, paths: set[str]) -> None:
        """Убрать файлы из «уже разобранных», чтобы конвейер снова их увидел."""
        for name in (SUGGESTIONS_NAME, "ollama_suggestions.json"):
            target = self.config["output_path"] / name
            if not target.exists():
                continue
            try:
                records = json.loads(target.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            kept = [rec for rec in records
                    if str(rec.get("Текущий путь", "")).replace("\\", "/") not in paths]
            if len(kept) != len(records):
                target.write_text(json.dumps(kept, ensure_ascii=False), "utf-8")

    @staticmethod
    def _min_conf(a: dict, key: str) -> float:
        val = a.get(key)
        if val is None:
            val = a.get("confidence", 0)
        try:
            return round(float(val), 2)
        except (TypeError, ValueError):
            return 0.0

    def _progressive_recheck_func(self, rows: list[dict], label: str):
        """Worker: построчно прогоняет разбор «факты → имя → место» (core.stages) и
        обновляет имя, папку и обе уверенности. Ничего не переносит — утверждает человек."""
        from core import stages

        targets = [dict(r) for r in rows]
        by_path = {str(r.get("Текущий путь", "")).replace("\\", "/"): r for r in rows}
        n = len(rows)

        def func() -> dict:
            done = errors = 0
            for i, snapshot in enumerate(targets, 1):
                rel = str(snapshot.get("Текущий путь", "")).replace("\\", "/")
                row = by_path.get(rel)
                if row is None:
                    continue
                self.log_event(f"🩺 {label} {i}/{n}: {rel.rpartition('/')[2]}")
                try:
                    answer = stages.refine_staged(
                        self.config, rel, previous=snapshot,
                        comment=str(snapshot.get("Комментарий", "") or ""),
                        log=self.log_event)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    self.log_event(f"  ✖ {type(exc).__name__}: {str(exc)[:160]}")
                    continue
                row["Новое имя"] = answer.get("new_name", row.get("Новое имя", ""))
                row["Целевой путь"] = answer.get("target_path", row.get("Целевой путь", ""))
                row["Уверенность имени"] = self._min_conf(answer, "name_confidence")
                row["Уверенность переноса"] = self._min_conf(answer, "move_confidence")
                row["Причина"] = (label + ": " + str(answer.get("reason", "")))[:400]
                row["Статус"] = "проверка"
                row["Применить"] = "FALSE"
                self.log_event(f"  → имя {row['Уверенность имени']:.2f} · папка {row['Уверенность переноса']:.2f}"
                               f" · {answer.get('seconds', '?')}с · {row['Целевой путь']}")
                done += 1
            self.save_map(silent=True)
            return {"обработано": done, "ошибок": errors}

        return func

    def recheck_selected(self) -> None:
        """🔬 Глубокая перепроверка выбранных строк: модель перечитывает файл ПОСТЕПЕННО
        (факты → имя → место) с бо́льшим контекстом и обновляет имя, папку и обе
        уверенности. Каждое решение всё равно за человеком."""
        rows = self.selected_rows()
        if not rows:
            review = [r for r in self.rows if str(r.get("Статус", "")).strip() == "проверка"]
            if review and messagebox.askyesno(
                    "Перепроверка",
                    f"Ничего не выделено. Перепроверить все {len(review)} строк со статусом "
                    "«проверка»? Модель разберёт каждый файл в три шага — это ~1–2 минуты на файл."):
                rows = review
            else:
                messagebox.showinfo("Перепроверка", "Выделите строки в карте (или Ctrl+A — все).")
                return
        if not messagebox.askyesno(
                "Перепроверка",
                f"Строк к перепроверке: {len(rows)}.\nМодель перечитает каждый файл в три шага "
                "(сначала факты, затем имя, затем место) и обновит имя, папку и обе уверенности; "
                "перенос по-прежнему утверждаете вы. Продолжить?"):
            return
        self.run_worker("🔬 Перепроверка (в три шага)",
                        self._progressive_recheck_func(rows, "перепроверка"))

    def keep_selected(self) -> None:
        """✔ «Не трогать» для выделенных строк: файл остаётся на месте, а строка
        уходит в «Историю» и больше не перечитывается моделью."""
        if self.showing_history:
            messagebox.showinfo("История", "Файл истории только для чтения.")
            return
        rows = self.selected_rows()
        if not rows:
            messagebox.showinfo("Не трогать", "Выделите строки в карте (или Ctrl+A — все).")
            return
        if not messagebox.askyesno(
                "Не трогать",
                f"Подтвердить «без изменений» для {len(rows)} файл(ов)?\n"
                "Файлы останутся на своих местах (переноса не будет), а строки уйдут "
                "в «Историю» и исчезнут из рабочей карты."):
            return
        chosen = {id(r) for r in rows}
        for r in rows:
            r["Статус"] = "без изменений"
            r["Применить"] = "FALSE"
            r["Тип операции"] = "без изменений"
            mapio.archive_manual(self.config["output_path"], r)
        self.rows = [r for r in self.rows if id(r) not in chosen]
        mapio.save_map(self.config["output_path"] / MAP_NAME, self.rows)
        self.load_map()
        self.log_event(f"✔ «не трогать»: {len(rows)} файл(ов) убрано в историю без переноса")

    def _apply_one(self, row: dict) -> None:
        """Применить перенос одного файла прямо из просмотрщика (доп. возможность)."""
        row["Применить"] = "TRUE"
        if (row.get("Статус") or "").strip() not in {"готово", "перенесено"}:
            row["Статус"] = "готово"
        # полная карта на диске — чтобы merge_back обновил именно эту строку, а не срез
        mapio.save_map(self.config["output_path"] / MAP_NAME, self.rows)
        try:
            result = applier.apply_plan(self.config, [row])
        except Exception as exc:  # noqa: BLE001
            self.load_map()
            messagebox.showerror("Перенос", f"Не удалось перенести файл:\n{type(exc).__name__}: {exc}",
                                 parent=self)
            return
        self.load_map()
        moved = int(result.get("перенесено", 0))
        errs = int(result.get("ошибок", 0))
        if errs:
            messagebox.showwarning("Перенос",
                                   "Файл не перенесён. Подробности — во вкладке «Журнал».", parent=self)
        elif moved:
            messagebox.showinfo("Перенос", "Файл перенесён и убран в «Историю».", parent=self)
        else:
            messagebox.showinfo("Перенос",
                                "Переносить нечего: имя и папка совпадают с текущими.\n"
                                "Если так и задумано — выберите «Не трогать».", parent=self)

    def audit_anomalous(self) -> None:
        """🩺 Аудит странного: эвристиками найти подозрительные строки карты и мягко
        перепроверить их моделью теми же тремя шагами. Текущие предложения не удаляются."""
        from core import audit
        from core import pipeline as _pl

        try:
            _snap, index = _pl._snapshot_and_index(self.config)
        except Exception:  # снимка нет — аудит без строгой проверки цели на карте
            index = None
        anomalies = audit.find_anomalies(self.rows, index)
        if not anomalies:
            messagebox.showinfo("Аудит",
                                "Странных строк не найдено: имена и цели выглядят согласованно.")
            return
        flagged = {a["rel"] for a in anomalies}
        rows = [r for r in self.rows
                if str(r.get("Текущий путь", "")).replace("\\", "/") in flagged]
        if not messagebox.askyesno(
                "Аудит странного",
                audit.summarize(anomalies) +
                f"\n\nПерепроверить эти {len(rows)} строк моделью (факты → имя → место)? "
                "Вы утвердите результат заново."):
            return
        self.run_worker("🩺 Аудит странного",
                        self._progressive_recheck_func(rows, "аудит"))

    def distill_lessons(self) -> None:
        """Модель анализирует карту (правки, отказы, комментарии) и выводит правила-черновики
        в learned_rules.md (в промпт попадают только подтверждённые «✓»)."""
        from core import lessons

        self.run_worker("Уроки из карты", lambda: lessons.distill(self.config, log=self.log_event))

    def run_folder_pass(self) -> None:
        """«Папка целиком»: модель видит все имена папки сразу и согласует имена друг с другом."""
        folder = filedialog.askdirectory(title="Выберите папку на диске отдела",
                                         initialdir=str(self.config["scan_root"]), parent=self)
        if not folder:
            return
        try:
            rel = Path(folder).relative_to(self.config["scan_root"]).as_posix()
        except ValueError:
            messagebox.showinfo("Вне диска отдела", "Нужна папка внутри диска отдела.", parent=self)
            return
        vision = messagebox.askyesno(
            "Зрение для картинок",
            f"Папка: {rel}\n\n«Да» — каждая картинка дополнительно пройдёт через зрение модели "
            "(медленнее, но именно так безымянные фото получают внятные имена).\n"
            "«Нет» — только общий запрос по списку имён.", parent=self)
        from core import folder_pass

        def job():
            return folder_pass.folder_pass(self.config, rel, limit=80, vision=vision,
                                           log=self.log_event)

        self.run_worker(f"Папка целиком: {rel}", job)

    def sweep_converted(self) -> None:
        """Список старых .doc/.rtf/.odt, рядом с которыми уже лежит .docx → на страницу «Корзина»."""
        def job():
            from core import cleanup, plugins
            rows = plugins.convertible_originals(self.config)
            cleanup.write_report(self.config, rows)
            return {"готовых оригиналов": len(rows)}

        self.run_worker("Поиск сконвертированных оригиналов", job)

    # ------------------------------------------------------------- корзина: выбор
    def trash_select_all(self) -> None:
        iids = list(self.trash_tree.get_children())
        self.trash_tree.selection_set(iids)
        self.status_var.set(f"выделено кандидатов: {len(iids)}")

    def show_trash_path(self) -> None:
        rows = self._trash_selected()
        if not rows:
            return
        row = rows[0]
        full = self.config["scan_root"] / str(row.get("Путь", ""))
        self.trash_path_box.configure(
            text=f"{row.get('Путь')}\nразмер {fmt_size(int(row.get('Размер') or 0))} · изменён "
                 f"{row.get('Изменён') or '—'} · признак: {row.get('Уверенность')} · {row.get('Причина')}\n"
                 f"полный путь: {full}")

    def _trash_full_path(self):
        rows = self._trash_selected()
        if not rows:
            messagebox.showinfo("Корзина", "Сначала выберите строку в списке (кликните по ней).")
            return None
        full = self.config["scan_root"] / str(rows[0].get("Путь", ""))
        if not full.exists():
            messagebox.showwarning("Корзина", f"Файла нет на месте — возможно, он уже перенесён:\n{full}")
            return None
        return full

    def reveal_trash(self) -> None:
        """Показать выбранный кандидат в проводнике (не открывая его)."""
        full = self._trash_full_path()
        if not full:
            return
        import subprocess
        import sys
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(full)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(full)])
            else:
                subprocess.Popen(["xdg-open", str(full.parent)])
        except Exception as exc:
            messagebox.showerror("Показать в папке", str(exc))

    def open_trash_file(self) -> None:
        """Открыть выбранный файл программой по умолчанию, чтобы проверить перед решением."""
        full = self._trash_full_path()
        if not full:
            return
        try:
            import os
            os.startfile(str(full))  # Windows: открыть связанной программой
        except Exception as exc:
            messagebox.showerror("Открыть файл", f"Не удалось открыть:\n{exc}")

    def sort_trash(self, column: str) -> None:
        """Сортировка корзины кликом по заголовку: размер — числом, путь — по алфавиту."""
        key = {"mark": "Отметить", "path": "Путь", "size": "__size", "kind": "Уверенность",
               "reason": "Причина"}.get(column, "Путь")
        self._trash_reverse = self._trash_sort == column and not self._trash_reverse
        self._trash_sort = column
        if key == "__size":
            self.trash_rows.sort(key=lambda row: int(row.get("Размер") or 0), reverse=self._trash_reverse)
        else:
            self.trash_rows.sort(key=lambda row: str(row.get(key, "")).casefold(), reverse=self._trash_reverse)
        heads = {"mark": "✓", "path": "Файл (полный путь)", "size": "Размер", "kind": "Признак",
                 "reason": "Почему предлагаю убрать"}
        for col, head in heads.items():
            arrow = (" ▲" if self._trash_reverse else " ▼") if col == column else ""
            self.trash_tree.heading(col, text=head + arrow)
        self.render_trash()

    # --------------------------------------------------------------- структура
    def load_structure(self) -> None:
        self.folder_tree.delete(*self.folder_tree.get_children())
        if not self.index:
            return
        limit = max(1, int(self.depth_var.get()))

        def walk(node, parent: str = "", depth: int = 0) -> None:
            if depth >= limit:
                return
            for child in node.children.values():
                self.folder_tree.insert(parent, "end", iid=child.path, text=child.name, values=(child.total,))
                walk(child, child.path, depth + 1)

        walk(self.index.root)

    # ------------------------------------------------------------------ промпт
    def current_prompt_path(self) -> Path:
        chosen = self.prompt_choice.get()
        for _, name, path in templates_info(self.config["app_dir"]):
            if name == chosen:
                return path
        return self.config["app_dir"] / "prompt.md"

    def load_prompt_editor(self) -> None:
        if not hasattr(self, "prompt_text"):
            return
        path = self.current_prompt_path()
        self.prompt_text.delete("1.0", "end")
        if path.exists():
            self.prompt_text.insert("1.0", path.read_text("utf-8"))
        text = self.prompt_text.get("1.0", "end").strip()
        self.prompt_info.configure(text=f"{path.name}: {len(text)} симв., ~{len(text) // 3} токенов")

    def save_prompt(self) -> None:
        path = self.current_prompt_path()
        path.write_text(self.prompt_text.get("1.0", "end").rstrip() + "\n", "utf-8")
        self.config = load_config(self.config["app_dir"])
        self.load_prompt_editor()
        self.append_log(f"сохранено: {path.name}", "ok")

    def restore_prompt(self) -> None:
        name = self.current_prompt_path().name
        if messagebox.askyesno("Образец", f"Заменить {name} заводским текстом?"):
            reset_template(self.config["app_dir"], name)
            self.load_prompt_editor()

    def open_prompt_file(self) -> None:
        open_path(self.current_prompt_path())

    def preview_prompt(self) -> None:
        rows = self.selected_rows()
        rel = simpledialog.askstring("Пример запроса", "Относительный путь файла:",
                                     initialvalue=rows[0].get("Текущий путь", "") if rows else "", parent=self)
        if not rel:
            return
        try:
            text = build_prompt_preview(self.config, rel.strip())
        except Exception as exc:
            messagebox.showerror("Просмотр промпта", f"{type(exc).__name__}: {exc}")
            return
        win = tk.Toplevel(self)
        win.title("Что уйдёт в модель")
        win.geometry("1040x700")
        win.configure(bg=T.CARD)
        box = tk.Text(win, wrap="word", font=("Cascadia Mono", 10), padx=12, pady=10, bg=T.CARD, fg=T.INK)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")

    def check_prompt(self) -> None:
        """Считаем честно: влезает ли промпт целиком и совпадает ли схема с текстом инструкции."""
        from core.config_loader import CHARS_PER_TOKEN, slice_budget
        from core.pipeline import estimate_tokens
        from core.prompts import RESPONSE_SCHEMA, build_system, templates_info

        app_dir = self.config["app_dir"]
        system = build_system(self.config, app_dir)
        llm = self.config["llm"]
        num_ctx = int(llm["num_ctx"])
        sys_tok = estimate_tokens(system)
        room = slice_budget(self.config, len(system))
        map_tok = int(room / CHARS_PER_TOKEN)
        file_tok = int(llm.get("content_chars", 6000) / CHARS_PER_TOKEN)
        answer_tok = int(llm.get("num_predict", 800))
        total = sys_tok + map_tok + file_tok + answer_tok
        fits = total <= num_ctx * 0.97

        lines = [
            f"Инструкция (prompt.md): {len(system)} симв. ≈ {sys_tok} ток.",
            f"Карта папок: до {room} симв. ≈ {map_tok} ток. (настроено "
            f"{self.config['folders']['budget_chars']}, глубина {self.config['folders']['depth']}, "
            f"не больше {self.config['folders']['max_paths']} путей)",
            f"Текст файла: до {llm.get('content_chars')} симв. ≈ {file_tok} ток.",
            f"Ответ модели: до {answer_tok} ток.",
            f"Итого ≈ {total} ток. против num_ctx = {num_ctx} → "
            + ("промт влезает целиком, обрезания нет" if fits else
               f"НЕ влезает: не хватает ~{total - num_ctx} ток. — увеличьте num_ctx до "
               f"{(total // 4096 + 1) * 4096} или уменьшите бюджет карты/текста"),
            "",
        ]
        start = system.find("ФОРМАТ ОТВЕТА")
        block = system[start:system.find("ПРИМЕРЫ")] if start >= 0 else ""
        required = list(RESPONSE_SCHEMA["required"])
        missing = [name for name in required if block and name not in block]
        extra = [name for name in RESPONSE_SCHEMA["properties"] if block and name not in block]
        if not block:
            lines.append("⚠ В инструкции нет раздела «ФОРМАТ ОТВЕТА» — модель может отвечать не по схеме.")
        else:
            named = [name for name in RESPONSE_SCHEMA["properties"] if name in block]
            lines.append(f"Схема ответа: в тексте описаны {len(named)} из {len(RESPONSE_SCHEMA['properties'])} полей "
                         f"(обязательных в схеме: {len(required)}).")
            if missing:
                lines.append("⚠ обязательные поля не описаны в тексте: " + ", ".join(missing))
            if extra:
                lines.append("· поле есть в схеме, но не упомянуто в тексте: " + ", ".join(extra))
        known = {"doc_types", "roots", "personal", "personal_root", "report_folder", "scope",
                 "mode_line", "folders_block", "rel_path", "name", "size", "modified", "kind",
                 "note_line", "content", "source_hint", "duplicates_block", "hint_line"}
        import re as _re

        for _title, filename, path in templates_info(app_dir):
            if not path.exists():
                continue
            found = set(_re.findall(r"\{([a-z_][a-z0-9_]*)\}", path.read_text("utf-8")))
            unknown = sorted(found - known)
            if unknown:
                lines.append(f"⚠ {filename}: подстановку {', '.join(unknown)} программа не знает — "
                             "она останется в промпте фигурными скобками")
        rows = self.selected_rows() if hasattr(self, "tree") else []
        if rows:
            rel = rows[0].get("Текущий путь", "")
            try:
                preview = build_prompt_preview(self.config, rel)
                head, _, _tail = preview.partition("=== USER")
                lines += ["", f"Запрос по файлу {rel}: всего {len(preview)} симв. "
                              f"≈ {estimate_tokens(preview)} ток. (инструкция в нём {len(head)} симв.)"]
                if estimate_tokens(preview) > num_ctx:
                    lines.append("⚠ этот запрос длиннее num_ctx — перед отправкой он будет сжат (см. журнал)")
            except Exception as exc:
                lines.append(f"(реальный запрос не собран: {type(exc).__name__}: {exc})")
        self._show_report("Проверка промпта", "\n".join(lines))
        self.append_log("проверка промпта: " + ("влезает целиком" if fits else f"не влезает ({total}/{num_ctx})"),
                        "ok" if fits else "note")

    def _show_report(self, title: str, text: str) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("900x560")
        win.configure(bg=T.CARD)
        box = tk.Text(win, wrap="word", font=("Cascadia Mono", 10), padx=12, pady=10, bg=T.CARD, fg=T.INK)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        T.selectable_copy(box)
        bar = tk.Frame(win, bg=T.CARD)
        bar.pack(fill="x")

        def copy_all() -> None:
            box.tag_add("sel", "1.0", "end-1c")
            box.event_generate("<Control-c>")

        T.Pill(bar, "Копировать всё", command=copy_all, style="ghost").pack(side="left", padx=12, pady=8)
        T.Pill(bar, "Закрыть", command=win.destroy, style="quiet").pack(side="left", padx=6)

    # ----------------------------------------------------------------- корзина
    def load_trash(self, silent: bool = False) -> None:
        self.trash_path_label.configure(text="Папка корзины: " + str(cleanup.trash_dir(self.config)))
        if not self.config.get("plugins", {}).get("quarantine", True):
            self.trash_rows = []
        else:
            try:
                records = mapio.load_records(self.config["output_path"] / SUGGESTIONS_NAME)
                self.trash_rows = cleanup.candidates(self.config) + cleanup.mark_from_model(records)
            except Exception as exc:
                if not silent:
                    messagebox.showerror("Корзина", str(exc))
                return
        self.render_trash()
        if not silent:
            self.append_log(f"кандидатов в корзину: {len(self.trash_rows)}", "note")

    def _trash_click(self, event) -> None:
        """Повторный клик по той же строке снимает выделение — иначе его «не убрать»."""
        if getattr(event, "detail", 1) > 1:
            return
        iid = self.trash_tree.identify_row(event.y)
        if iid and iid == self._trash_last_iid and len(self.trash_tree.selection()) <= 1:
            self.trash_tree.selection_set(())
            self._trash_last_iid = ""
            self.status_var.set("выделение снято")
            return
        self._trash_last_iid = iid

    def _trash_double(self, event) -> str:
        """Двойной клик — переключить отметку именно этой строки."""
        iid = self.trash_tree.identify_row(event.y)
        if not iid or not iid.isdigit():
            return "break"
        row = self.trash_rows[int(iid)]
        row["Отметить"] = "FALSE" if (row.get("Отметить") or "").upper() == "TRUE" else "TRUE"
        self.render_trash()
        self.trash_tree.selection_set(iid)
        self._trash_last_iid = iid
        return "break"

    def trash_clear_selection(self) -> None:
        self.trash_tree.selection_set(())
        self._trash_last_iid = ""
        self.status_var.set("выделение снято")

    def render_trash(self) -> None:
        self.trash_tree.delete(*self.trash_tree.get_children())
        for index, row in enumerate(self.trash_rows):
            self.trash_tree.insert("", "end", iid=str(index), values=(
                "☑" if (row.get("Отметить") or "").upper() == "TRUE" else "☐",
                row.get("Путь"), fmt_size(int(row.get("Размер") or 0)),
                row.get("Причина"), row.get("Уверенность")))
        marked = sum(1 for row in self.trash_rows if (row.get("Отметить") or "").upper() == "TRUE")
        self.status_var.set(f"кандидатов: {len(self.trash_rows)}, отмечено: {marked}")

    def _trash_selected(self) -> list[dict]:
        return [self.trash_rows[int(iid)] for iid in self.trash_tree.selection() if iid.isdigit()]

    def trash_mark(self, value: bool) -> None:
        for row in self._trash_selected():
            row["Отметить"] = "TRUE" if value else "FALSE"
        self.render_trash()

    def trash_toggle(self) -> None:
        rows = self._trash_selected()
        value = any((row.get("Отметить") or "").upper() != "TRUE" for row in rows)
        for row in rows:
            row["Отметить"] = "TRUE" if value else "FALSE"
        self.render_trash()

    def move_trash(self) -> None:
        marked = [row for row in self.trash_rows if (row.get("Отметить") or "").upper() == "TRUE"]
        if not marked:
            messagebox.showinfo("Корзина", "Отметьте строки пробелом или двойным щелчком.")
            return
        target = cleanup.trash_dir(self.config)
        if not messagebox.askyesno(
            "Перенести в корзину",
            f"ФАЙЛЫ НЕ УДАЛЯЮТСЯ.\n\n{len(marked)} файл(ов) будут ПЕРЕМЕЩЕНЫ в:\n{target}\n\n"
            "Оттуда вы удалите их вручную, когда проверите. Действие пишется в журнал и откатывается."):
            return
        if simpledialog.askstring("Подтверждение", "Введите УБРАТЬ:", parent=self) != "УБРАТЬ":
            return
        self.run_worker("Перенос в корзину", lambda: cleanup.move_to_trash(
            self.config, marked, progress=lambda i, n, rel: self.events.put(("progress", i, n, "Корзина"))))

    # ----------------------------------------------------------------- плагины
    def load_plugins(self) -> None:
        self.plugin_tree.delete(*self.plugin_tree.get_children())
        for row in plugins.status(self.config):
            enabled = "—" if row["включено"] is None else ("да" if row["включено"] else "нет")
            self.plugin_tree.insert("", "end", iid=row["id"],
                                    tags=("ok" if row["установлено"] else "miss",),
                                    values=(row["название"], "установлено" if row["установлено"] else row["подробно"],
                                            enabled, row["зачем"],
                                            "не требуется" if row["установлено"] else row["команда"]))

    def install_plugin(self) -> None:
        selected = self.plugin_tree.selection()
        if not selected:
            messagebox.showinfo("Плагины", "Выберите возможность в списке.")
            return
        plugin_id = selected[0]

        def worker():
            ok = plugins.install(plugin_id, log=self.log_event, config=self.config)
            spec = next((p for p in plugins.registry() if p.id == plugin_id), None)
            if ok and spec and spec.enabled_key:
                self.config.setdefault("plugins", {})[spec.enabled_key] = True
                save_config(self.config)
                self.config = load_config(self.config["app_dir"])
            elif not ok and spec and spec.note:
                self.log_event(f"нужно дополнительно: {spec.note}")
            return {"готово": "да" if ok else "нет"}

        self.run_worker("Установка плагина", worker)

    def verify_plugin(self) -> None:
        selected = self.plugin_tree.selection()
        if not selected:
            messagebox.showinfo("Плагины", "Выберите возможность в списке.")
            return
        plugin_id = selected[0]
        name = next((p.name for p in plugins.registry() if p.id == plugin_id), plugin_id)

        def worker():
            lines = plugins.verify(plugin_id, self.config, log=self.log_event)
            ready = all("НЕ найден" not in line and "нет —" not in line for line in lines)
            return {"готово": "да" if ready else "нет", "строк отчёта": len(lines)}

        self.run_worker(f"Проверка: {name}", worker)

    def toggle_plugin(self) -> None:
        selected = self.plugin_tree.selection()
        if not selected:
            return
        spec = next((p for p in plugins.registry() if p.id == selected[0]), None)
        if spec is None or not spec.enabled_key:
            messagebox.showinfo("Плагины", "Здесь переключатель не нужен.")
            return
        turn_on = not bool(self.config.get("plugins", {}).get(spec.enabled_key))
        self.config.setdefault("plugins", {})[spec.enabled_key] = turn_on
        save_config(self.config)
        self.config = load_config(self.config["app_dir"])
        self.load_plugins()
        self.append_log(f"{spec.name}: {'включено' if turn_on else 'выключено'}", "note")

    def run_broken(self) -> None:
        def work():
            report = plugins.find_broken(self.config,
                                         progress=lambda n, c: self.events.put(("progress", c, c + 1, "Проверка")))
            return {"проверено": report["проверено"], "подозрительных": report["подозрительных"]}

        self.run_worker("Поиск битых файлов", work)

    def run_convert(self, apply: bool) -> None:
        if apply and not messagebox.askyesno("Конвертация",
                                             "Рядом со старыми файлами создам .docx. Оригиналы не трогаю. Продолжить?"):
            return
        self.run_worker("Конвертация" if apply else "Список к конвертации",
                        lambda: plugins.convert_legacy(self.config, apply=apply, limit=200,
                                                       progress=lambda n, c: self.events.put(("progress", c, c + 1, "Файлы"))))

    def run_repair(self, apply: bool) -> None:
        if not apply:
            def job():
                import csv as _csv
                rows = plugins.name_repairs(self.config)
                target = self.config["output_path"] / "name_repairs.csv"
                fields = sorted({k for row in rows for k in row}) or ["Путь", "Новое имя"]
                with target.open("w", newline="", encoding="utf-8-sig") as stream:
                    writer = _csv.DictWriter(stream, fieldnames=fields, delimiter=";", extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)
                self.events.put(("log", f"вывод предложений по именам: {target}"))
                return {"предложений": len(rows), "файл": target.name}
            self.run_worker("Предложения по именам", job)
            return
        rows = plugins.name_repairs(self.config)
        if not messagebox.askyesno("Починка имён",
                                   f"Переименую до {len(rows)} объектов. Удалений нет, конфликты пропускаю. Продолжить?"):
            return
        self.run_worker("Починка имён", lambda: plugins.apply_name_repairs(self.config, log=self.log_event))

    # --------------------------------------------------------------- настройки
    def pick(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
        if chosen:
            variable.set(chosen)

    def _update_context_line(self, *_args) -> None:
        """Показать, из чего складывается контекст, и влезает ли промпт целиком."""
        if not hasattr(self, "context_line"):
            return
        from core.config_loader import CHARS_PER_TOKEN, slice_budget
        from core.prompts import build_system

        try:
            num_ctx = int(self.svars["num_ctx"].get())
            content = int(self.svars["content_chars"].get())
            budget = int(self.svars["budget_chars"].get())
        except (ValueError, tk.TclError):
            return
        try:
            system_chars = len(build_system(self.config, self.config["app_dir"]))
        except Exception:
            system_chars = int(self.config.get("system_prompt_chars") or 30000)
        sys_tok = int(system_chars / CHARS_PER_TOKEN)
        file_tok = int(content / CHARS_PER_TOKEN)
        answer_tok = int(self.config["llm"].get("num_predict", 800))
        room = slice_budget({**self.config, "llm": {**self.config["llm"], "num_ctx": num_ctx},
                             "folders": {**self.config["folders"], "budget_chars": budget,
                                         "content_chars": content}}, system_chars, content)
        total = sys_tok + int(room / CHARS_PER_TOKEN) + file_tok + answer_tok
        fits = total <= num_ctx
        self.context_line.configure(
            fg=(T.GOOD if fits else T.WARN),
            text=(f"контекст: инструкция ~{sys_tok} ток. ({system_chars} симв.) + карта ≤{room} симв. "
                  f"(~{int(room / CHARS_PER_TOKEN)}) + файл ≤{file_tok} + ответ ≤{answer_tok} "
                  f"= ~{total} из num_ctx={num_ctx} → "
                  + ("промт целиком влезает, обрезания нет" if fits else
                     f"НЕ влезает: не хватает {total - num_ctx} ток. — увеличьте num_ctx до "
                     f"{int((total // 1024) + 1) * 1024}")))

    def save_settings(self) -> None:
        old_lang = str(self.config.get("ui_lang", "ru"))
        try:
            roots = [r.strip() for r in self.svars["roots"].get().split(";") if r.strip()]
            if roots:
                if str(self.svars["scan_root"].get().strip()).casefold() == str(self.config["mo_root"]).casefold():
                    self.svars["scan_root"].set(roots[0])
                self.config["mo_root"] = Path(roots[0])
                self.config["root_candidates"] = roots[1:]
            self.config["auto_pick_root"] = bool(self.svars["auto_pick"].get())
            self.config["scan_root"] = Path(self.svars["scan_root"].get())
            self.config["output_path"] = Path(self.svars["output_dir"].get())
            self.config["output_dir"] = str(self.config["output_path"])
            self.config["quarantine_dir"] = self.svars["quarantine"].get().strip()
            llm = self.config["llm"]
            llm.update({
                "model_text": self.svars["model"].get().strip(),
                "model_vision": self.svars["vision_model"].get().strip(),
                "base_url": self.svars["base_url"].get().strip() or "http://127.0.0.1:11434",
                "num_ctx": int(self.svars["num_ctx"].get()),
                "vision_num_ctx": int(self.svars["vision_num_ctx"].get()),
                "content_chars": int(self.svars["content_chars"].get()),
                "cache_policy": self.svars["cache"].get(),
                "vision": bool(self.svars["vision"].get()),
            })
            folders = self.config["folders"]
            folders.update({
                "budget_chars": int(self.svars["budget_chars"].get()),
                "max_paths": int(self.svars["max_paths"].get()),
                "depth": int(self.svars["depth"].get()),
                "hierarchical": bool(self.svars["hierarchical"].get()),
                "hide_prefixes": [p.strip() for p in self.svars["hide_prefixes"].get().split(";") if p.strip()],
            })
            lang_name = self.svars["ui_lang"].get()
            from ui import i18n as _I
            code = next((c for c, n in _I.LANGS.items() if n == lang_name), "ru")
            self.config["ui_lang"] = code
            llm["embed_model"] = self.svars["embed_model"].get().strip()
            self.config.setdefault("queue", {})["rules_first"] = bool(self.svars["rules_first"].get())
            self.config["thresholds"]["min_confidence"] = float(self.svars["confidence"].get())
            self.config["thresholds"]["min_name_confidence"] = float(self.svars["conf_name"].get())
            self.config["thresholds"]["min_move_confidence"] = float(self.svars["conf_move"].get())
            self.config["thresholds"]["new_folder_max_confidence"] = float(self.svars["new_folder_cap"].get())
            self.config.setdefault("plugins", {}).update({
                "ocr": bool(self.svars["ocr"].get()),
                "tesseract_cmd": self.svars["tesseract"].get().strip(),
                "media_info": bool(self.svars["media_info"].get()),
                "doc_convert": bool(self.svars["doc_convert"].get()),
                "quarantine": bool(self.svars["quarantine_on"].get()),
            })
            save_config(self.config)
            self.config = load_config(self.config["app_dir"])
            self._update_context_line()
            for problem in self.config.get("problems", []):
                self.append_log("внимание: " + problem, "note")
            self.append_log("настройки сохранены", "ok")
            self.refresh_all()
            if code != old_lang:
                if messagebox.askyesno(
                        "Язык интерфейса",
                        "Новый язык применится после перезапуска.\nПерезапустить программу сейчас?"):
                    self.restart_app()
        except Exception as exc:
            messagebox.showerror("Настройки", f"{type(exc).__name__}: {exc}")

    def restart_app(self) -> None:
        import subprocess
        import sys
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable] + sys.argv[1:])
        else:
            subprocess.Popen([sys.executable,
                              str(Path(__file__).resolve().parent.parent / "app.py")])
        self.destroy()

    def run_doctor(self) -> None:
        import app as cli

        lines: list[str] = []
        original = cli.say
        cli.say = lambda message="": lines.append(str(message))
        try:
            code = cli.cmd_doctor(self.config)
        finally:
            cli.say = original
        win = tk.Toplevel(self)
        win.title("Диагностика")
        win.geometry("900x560")
        win.configure(bg=T.CARD)
        box = tk.Text(win, wrap="word", font=("Cascadia Mono", 10), padx=12, pady=10, bg=T.CARD, fg=T.INK)
        box.pack(fill="both", expand=True)
        box.insert("1.0", "\n".join(lines) + f"\n\nкод возврата: {code}")
        box.configure(state="disabled")

    def check_ollama(self) -> None:
        from core.llm import OllamaClient

        def worker() -> None:
            client = OllamaClient(base_url=self.config["llm"]["base_url"], model=self.config["llm"]["model_text"])
            alive = client.is_alive()
            self.events.put(("ollama", alive, client.list_models() if alive else []))

        threading.Thread(target=worker, daemon=True).start()

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Работа идёт", "Остановить после текущего файла и закрыть окно?"):
                return
            self.stop_work()
        self.destroy()

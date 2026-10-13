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
from ui import input as uinput
from ui import theme as T
from ui.dialogs import open_path, reveal_path, text_window
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
# Разделы без своих вкладок: открываются кнопками на странице «Настройки», сами страницы сохранены
HIDDEN_PAGES = ("prompt", "plugins", "guide")

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


class OrganizerApp(tk.Tk):
    def __init__(self, config: dict):
        super().__init__()
        T.reset_caches()
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
            T._prune_items(self.dot)
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
        """Строка подписи = ширина контейнера: текст не уезжает за правый край."""
        parent = label.master

        def resync(_event=None) -> None:
            try:
                width = max(240, int(parent.winfo_width()) - 12 - extra)
                label.configure(wraplength=width)
            except tk.TclError:
                pass

        parent.bind("<Configure>", resync, add="+")
        label.after(80, resync)
        return label

    # ------------------------------------------------------------------ сборка
    def _build(self) -> None:
        self.gradient = T.Gradient(self)
        self.gradient.place(x=0, y=0, relwidth=1, relheight=1)

        # правая часть шапки упакована ПЕРВОЙ: ей достаётся место раньше длинной подписи слева
        header = tk.Frame(self, bg=T.BG_TOP)
        header.pack(fill="x", padx=24, pady=(14, 4))
        right = tk.Frame(header, bg=T.BG_TOP)
        right.pack(side="right", anchor="n", padx=(12, 0))
        status_row = tk.Frame(right, bg=T.BG_TOP)
        status_row.pack(anchor="e")
        self.dot = tk.Canvas(status_row, width=16, height=16, bg=T.BG_TOP, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 5))
        self.set_dot("#9aa4b2")
        self.ollama_label = tk.Label(status_row, text="Локальная модель: проверка…", bg=T.BG_TOP, fg=T.MUTED,
                                     font=("Segoe UI", 11), anchor="e", justify="right")
        self.ollama_label.pack(side="left")
        self.ollama_toggle = T.Pill(right, "Ollama: выключить", command=self.toggle_ollama, style="ghost", icon="⏻")
        self.ollama_toggle.pack(anchor="e", pady=(4, 0))
        T.Pill(right, "Открыть результаты", command=lambda: open_path(self.config["output_path"]),
               style="ghost", icon="📂").pack(anchor="e", pady=(6, 0))
        title = tk.Frame(header, bg=T.BG_TOP)
        title.pack(side="left", fill="x", expand=True)
        tk.Label(title, text="Архивариус", bg=T.BG_TOP, fg=T.INK,
                 font=("Segoe UI Semibold", 26)).pack(anchor="w")
        tagline = tk.Label(title, text="читает содержимое · предлагает имя и папку · переносит только "
                                       "утверждённое", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 11),
                           justify="left", anchor="w")
        tagline.pack(anchor="w", fill="x")
        title.bind("<Configure>", lambda e: tagline.configure(wraplength=max(240, e.width - 8)))

        from ui import i18n as I
        self.tabbar = T.TabBar(self, [(k, I.tr(title)) for k, title in PAGES if k not in HIDDEN_PAGES], command=self._show_page, bg=T.BG_TOP)
        self.tabbar.pack(fill="x", padx=24, pady=(4, 6))

        # строка состояния — до контейнера: на тесном окне именно страница уступает место, а не статус
        self.status_var = tk.StringVar(value="Готово")
        status = tk.Frame(self, bg=T.BG_TOP)
        status.pack(side="bottom", fill="x", padx=24, pady=(4, 8))
        tk.Label(status, textvariable=self.status_var, bg=T.BG_TOP, fg=T.MUTED,
                 font=("Segoe UI", 10), anchor="w").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=100)
        self.progress.pack(side="right", fill="x", expand=True, padx=(20, 0))

        self.container = tk.Frame(self, bg=T.BG_TOP)
        self.container.pack(fill="both", expand=True, padx=24)
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
        uinput.install(self)
        self._show_page("home")

    def _show_page(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        page.place(x=0, y=0, relwidth=1, relheight=1)
        page.tkraise()
        page.scroll_to_top()
        self._current_page = key
        T.fade_in(page.body, T.ACCENT_SOFT, T.BG_TOP)

    def _card(self, parent, title: str = "", tone: str = T.CARD) -> tk.Frame:
        from ui import i18n as _I
        title = _I.tr(title)
        card = T.Card(parent, padding=14, tone=tone)
        card.pack(fill="x", pady=(0, 12))
        if title:
            tk.Label(card.inner, text=title, bg=tone, fg=T.INK,
                     font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
        return card.inner

    def _toolbar(self, parent, bg: str = T.BG_TOP, pady=(0, 8)) -> T.Flow:
        """Панель кнопок с переносом строк: на узком окне кнопки не уезжают за край."""
        bar = T.Flow(parent, bg=bg)
        bar.pack(fill="x", pady=pady)
        return bar

    def _btn(self, bar: T.Flow, text: str, command, style: str = "ghost", **kw) -> T.Pill:
        pill = T.Pill(bar, text, command=command, style=style, bg=bar["bg"], **kw)
        bar.add(pill)
        return pill

    def _more_menu(self, bar: T.Flow, label: str, items: list) -> T.Pill:
        """Кнопка «⋯ Ещё»: редкие действия в выпадающем меню — панель не раздувается."""
        menu = tk.Menu(self, tearoff=0, bg=T.CARD, fg=T.INK, activebackground=T.ACCENT,
                       activeforeground="#ffffff", relief="flat", borderwidth=1, font=("Segoe UI", 10))
        for text, command in items:
            menu.add_command(label=text, command=command)

        def post() -> None:
            try:
                menu.tk_popup(pill.winfo_rootx(), pill.winfo_rooty() + pill.winfo_height())
            finally:
                menu.grab_release()

        pill = self._btn(bar, label, post, "quiet")
        return pill

    def _note(self, bar: T.Flow, text: str, **kw) -> tk.Label:
        """Пояснение внутри панели: занимает всю ширину отдельной строкой."""
        label = tk.Label(bar, text=text, bg=bar["bg"], fg=T.MUTED, font=("Segoe UI", 10),
                         justify="left", anchor="w", **kw)
        bar.add(label, stretch=True)
        return label

    def _table(self, parent, columns, heads: dict, widths: dict, stretch: set, *, show="headings",
               selectmode="extended", height: int = 8, fill: bool = True, pady=(4, 10)):
        """Таблица в карточке: растёт на всю свободную высоту, полосы прокрутки по обеим осям."""
        card = T.Card(parent, padding=8)
        card.pack(fill="both" if fill else "x", expand=fill, pady=pady)
        holder = card.inner
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        tree = ttk.Treeview(holder, columns=columns, show=show, selectmode=selectmode, height=height)
        ybar = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        for col in columns:
            tree.column(col, width=widths.get(col, 100), minwidth=40, stretch=col in stretch)
            if col in heads:
                tree.heading(col, text=heads[col])
        return card, tree

    def _text_box(self, parent, *, height: int = 8, font=("Segoe UI", 10), dark: bool = False,
                  fill: bool = True, readonly: bool = True, pady=(0, 0)) -> tk.Text:
        """Текстовое поле с полосой прокрутки; read-only поля выделяются и копируются."""
        holder = tk.Frame(parent, bg=T.CARD)
        holder.pack(fill="both" if fill else "x", expand=fill, pady=pady)
        colors = dict(bg="#101828", fg="#e4e7ec", insertbackground="white") if dark else \
            dict(bg=T.CARD, fg=T.INK, insertbackground=T.INK)
        box = tk.Text(holder, wrap="word", height=height, font=font, relief="flat", padx=12, pady=8, **colors)
        bar = ttk.Scrollbar(holder, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)
        if readonly:
            T.selectable_copy(box)
        return box

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
        """Широкие подписи (wraplength ≥ 600) переносятся по ширине КОНТЕЙНЕРА, а не по своей собственной.

        Раньше ширина подписи брала сама себя и схлопывалась до 260 px — длинные тексты
        превращались в узкую колонку. Узкие пояснения рядом с полями остаются как есть."""
        for widget in self._descendants():
            if not isinstance(widget, tk.Label):
                continue
            try:
                if int(widget.cget("wraplength") or 0) < 600:
                    continue
            except (ValueError, tk.TclError):
                continue
            self._wrap(widget)

    def _make_texts_copyable(self) -> None:
        """Журнал, инструкция и подробности строки выделяются и копируются (Ctrl+C, правый клик).

        disabled-поле в tkinter не выделяется мышью, поэтому поля остаются обычными,
        а ввод перехватывается — случайно «испортить» текст нельзя."""
        editable = {id(self.prompt_text)} if hasattr(self, "prompt_text") else set()
        for widget in self._descendants():
            if not isinstance(widget, tk.Text) or id(widget) in editable or getattr(widget, "_ro_guard", False):
                continue
            try:
                widget.configure(state="normal")
                T.selectable_copy(widget)
            except tk.TclError:
                continue

    def _enable_clipboard_shortcuts(self) -> None:
        uinput.install(self)

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
        # слева направо — порядок работы: отметить, утвердить, проверить, перенести; справа — прочее
        bar = self._toolbar(parent, pady=(0, 2))
        self._btn(bar, "☑ Отметить", lambda: self.set_apply(True), "good")
        self._btn(bar, "☐ Снять", lambda: self.set_apply(False))
        self._btn(bar, "☑ Выделить все", self.select_all_rows, "quiet")
        self._btn(bar, "✅ Утвердить помеченные", self.approve_marked)
        self._btn(bar, "🧪 Пробный перенос", self.show_dry_run)
        self._btn(bar, "🚚 Применить перенос", self.apply_marked, "accent")
        self._btn(bar, "👁 Смотреть", self.view_selected, "quiet")
        self._btn(bar, "🗑 В корзину выделенное", self.trash_selected_map_rows, "danger")

        tools = tk.Frame(parent, bg=T.BG_TOP)
        tools.pack(fill="x", pady=(4, 6))
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

        bar2 = self._toolbar(parent, pady=(0, 4))
        self._btn(bar2, "🔁 Переразобрать выбранное", self.requeue_selected)
        self._btn(bar2, "🔬 Перепроверить глубже", self.recheck_selected)
        self._btn(bar2, "📦 Папка целиком", self.run_folder_pass, "accent")
        self._btn(bar2, "✔ Не трогать", self.keep_selected, "quiet")
        self._more_menu(bar2, "⋯ Ещё", [
            ("🩺 Проверить странное", self.audit_anomalous),
            ("🧠 Уроки из правок", self.distill_lessons),
            ("🔄 Перечитать карту с диска", self.load_map),
            ("💾 Сохранить карту", self.save_map),
        ])

        columns = ("apply", "status", "conf_name", "conf_move", "current", "new", "target", "op")
        widths = {"apply": 40, "status": 84, "conf_name": 56, "conf_move": 56,
                  "current": 300, "new": 300, "target": 280, "op": 150}
        heads = {"apply": "✓", "status": "Статус", "conf_name": "Имя", "conf_move": "Папка",
                 "current": "Было (текущий путь)", "new": "Станет", "target": "Куда", "op": "Действие"}
        _card, self.tree = self._table(parent, columns, heads, widths, {"current", "new", "target"},
                                       height=7)
        for col in columns:
            self.tree.heading(col, command=lambda c=col: self.sort_rows(c))
        for tag, color in (("ready", T.GOOD), ("review", T.WARN), ("error", T.BAD), ("done", T.MUTED)):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<Double-1>", lambda _e: self.view_selected())
        self.tree.bind("<Return>", lambda _e: self.view_selected())
        self.tree.bind("<space>", lambda _e: self.toggle_apply())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        uinput.attach_tree(self.tree, on_select_all=self.select_all_rows, extra=[
            ("Копировать «Было» (путь)", lambda: self._copy_column(self.tree, "current")),
            ("Копировать «Станет» (имя)", lambda: self._copy_column(self.tree, "new")),
            ("Копировать «Куда» (папка)", lambda: self._copy_column(self.tree, "target")),
        ])

        detail = T.Card(parent, padding=10, tone=T.CARD_SOFT)
        detail.pack(fill="x")
        head = tk.Frame(detail.inner, bg=T.CARD_SOFT)
        head.pack(fill="x")
        tk.Label(head, text="Полные данные строки — без обрезки", bg=T.CARD_SOFT, fg=T.MUTED,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        T.Pill(head, "📋 Копировать", command=self.copy_details, style="quiet",
               bg=T.CARD_SOFT).pack(side="right")
        self.details_box = self._text_box(detail.inner, height=6, fill=False, pady=(4, 0))

    def _copy_column(self, tree: ttk.Treeview, column: str) -> None:
        """Значения одного столбца у выделенных строк — по строке на файл (путь, имя, папка)."""
        values = [str(tree.set(iid, column)) for iid in tree.selection()]
        if values:
            uinput.put_on_clipboard(tree, "\n".join(values))
            self.status_var.set(f"скопировано строк: {len(values)}")

    def copy_details(self) -> None:
        text = self.details_box.get("1.0", "end-1c").strip()
        if text:
            uinput.put_on_clipboard(self.details_box, text)
            self.status_var.set("подробности строки скопированы")

    # --------------------------------------------------------------- структура
    def _build_tree(self, parent) -> None:
        bar = self._toolbar(parent)
        bar.add(tk.Label(bar, text="Глубина", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)))
        self.depth_var = tk.IntVar(value=int(self.config["folders"].get("depth", 5)))
        spin = ttk.Spinbox(bar, from_=1, to=9, width=4, textvariable=self.depth_var, command=self.load_structure)
        bar.add(spin)
        self._btn(bar, "Развернуть всё", lambda: self._tree_open(True), "quiet")
        self._btn(bar, "Свернуть", lambda: self._tree_open(False), "quiet")
        self._note(bar, "Глубина = сколько ярусов раскрывать. Модель видит карту системы до настроенной "
                        "глубины, кроме скрытых ветвей («Разобрать»).")

        actions = self._toolbar(parent)
        self._btn(actions, "📑 Аудит структуры", lambda: self._open_name("structure_audit.md"), "accent")
        self._btn(actions, "📂 Папки", self.open_folders_window, "good")
        self._btn(actions, "📄 План папок", self.make_plan)
        self._btn(actions, "Создать папки из плана", self.create_plan, "good")
        self.plan_note = self._note(actions, "план лежит в structure_plan.txt — правьте его перед созданием")

        _card, self.folder_tree = self._table(parent, ("files",), {"files": "Файлов в ветке"},
                                              {"files": 120}, set(), show="tree headings",
                                              selectmode="browse", height=8, pady=(0, 10))
        self.folder_tree.heading("#0", text="Папка")
        self.folder_tree.column("#0", width=420, minwidth=180, stretch=True)
        self.folder_tree.column("files", width=120, minwidth=90, anchor="e", stretch=False)
        uinput.attach_tree(self.folder_tree, extra=[
            ("Копировать путь папки", lambda: uinput.put_on_clipboard(
                self.folder_tree, "\n".join(self.folder_tree.selection()))),
        ])

    def _tree_open(self, value: bool) -> None:
        stack = list(self.folder_tree.get_children(""))
        while stack:
            iid = stack.pop()
            self.folder_tree.item(iid, open=value)
            stack.extend(self.folder_tree.get_children(iid))

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
        bar = self._toolbar(parent)
        bar.add(tk.Label(bar, text="Файл", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 10)))
        self.templates = templates_info(self.config["app_dir"])
        self.prompt_choice = tk.StringVar(value=self.templates[0][1])
        combo = ttk.Combobox(bar, textvariable=self.prompt_choice, state="readonly", width=26,
                             values=[name for _, name, _ in self.templates])
        bar.add(combo)
        self.prompt_choice.trace_add("write", lambda *_: self.load_prompt_editor())
        self._btn(bar, "💾 Сохранить", self.save_prompt, "accent")
        self._btn(bar, "Образец", self.restore_prompt)
        self._btn(bar, "Открыть в редакторе", self.open_prompt_file)
        self._btn(bar, "🔎 Проверить промпт", self.check_prompt, "good")
        self._btn(bar, "Показать запрос строки", self.preview_prompt, "quiet")
        self.prompt_info = self._note(bar, "")
        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        self.prompt_text = self._text_box(card.inner, height=10, font=("Cascadia Mono", 10), readonly=False)
        self.prompt_text.configure(insertbackground=T.INK, undo=True)
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
        self.trash_path_label = tk.Label(intro, bg=T.CARD, fg=T.INK, font=("Segoe UI Semibold", 10), anchor="w",
                                         wraplength=1180, justify="left")
        self.trash_path_label.pack(anchor="w", pady=(4, 0))

        bar = self._toolbar(parent)
        self._btn(bar, "🔄 Обновить список", self.load_trash, "accent")
        self._btn(bar, "Отметить выбранное", lambda: self.trash_mark(True))
        self._btn(bar, "Снять отметку", lambda: self.trash_mark(False))
        self._btn(bar, "☑ Выделить все", self.trash_select_all, "quiet")
        self._btn(bar, "Убрать выделение", self.trash_clear_selection)
        self._btn(bar, "Перенести в корзину", self.move_trash, "danger")
        self._btn(bar, "📂 Показать в папке", self.reveal_trash)
        self._btn(bar, "Открыть файл", self.open_trash_file)
        self._btn(bar, "Открыть CSV", lambda: self._open_name("trash_candidates.csv"))

        columns = ("mark", "path", "size", "reason", "kind")
        heads = {"mark": "✓", "path": "Файл (полный путь)", "size": "Размер", "kind": "Признак",
                 "reason": "Почему предлагаю убрать"}
        widths = {"mark": 40, "path": 420, "size": 84, "kind": 100, "reason": 340}
        _card, self.trash_tree = self._table(parent, columns, heads, widths, {"path", "reason"},
                                             height=7, pady=(0, 8))
        self._trash_sort, self._trash_reverse = "", False
        for col in columns:
            self.trash_tree.heading(col, command=lambda c=col: self.sort_trash(c))
        self.trash_tree.bind("<space>", lambda _e: self.trash_toggle())
        self.trash_tree.bind("<Return>", lambda _e: self.reveal_trash())
        self.trash_tree.bind("<Double-1>", self._trash_double, add="+")
        self.trash_tree.bind("<Button-1>", self._trash_click, add="+")
        self.trash_tree.bind("<<TreeviewSelect>>", lambda _e: self.show_trash_path())
        self._trash_last_iid = ""
        uinput.attach_tree(self.trash_tree, on_select_all=self.trash_select_all, extra=[
            ("Копировать полный путь", self.copy_trash_paths),
        ])

        hint = T.Card(parent, padding=10, tone=T.CARD_SOFT)
        hint.pack(fill="x")
        self.trash_path_box = self._text_box(hint.inner, height=3, fill=False, font=("Segoe UI", 10))
        self.trash_path_box.configure(bg=T.CARD_SOFT)
        self._set_ro_text(self.trash_path_box, "Клик по строке — здесь появится полный путь к файлу "
                                               "(его можно выделить и скопировать).")

    def _set_ro_text(self, box: tk.Text, text: str) -> None:
        """Записать текст в поле «только для чтения» (оно остаётся выделяемым и копируемым)."""
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)

    def copy_trash_paths(self) -> None:
        full = [str(self.config["scan_root"] / str(r.get("Путь", ""))) for r in self._trash_selected()]
        if full:
            uinput.put_on_clipboard(self.trash_tree, "\n".join(full))
            self.status_var.set(f"скопировано путей: {len(full)}")

    # ----------------------------------------------------------------- плагины
    def _build_plugins(self, parent) -> None:
        bar = self._toolbar(parent)
        self._btn(bar, "🔄 Проверить", self.load_plugins, "accent")
        self._btn(bar, "Проверить выбранное", self.verify_plugin, "good")
        self._btn(bar, "Установить выбранное", self.install_plugin)
        self._btn(bar, "Включить / выключить", self.toggle_plugin)
        self._note(bar, "pip ставит библиотеки; внешние программы (Tesseract, ffmpeg) проверяем отдельно — "
                        "кнопкой «Проверить выбранное»")
        columns = ("title", "state", "enabled", "why", "how")
        heads = {"title": "Возможность", "state": "Статус", "enabled": "Включено", "why": "Что даёт",
                 "how": "Как поставить"}
        widths = {"title": 200, "state": 220, "enabled": 84, "why": 340, "how": 260}
        _card, self.plugin_tree = self._table(parent, columns, heads, widths, {"why", "how"},
                                              selectmode="browse", height=6, pady=(0, 8))
        for tag, color in (("ok", T.GOOD), ("miss", T.WARN)):
            self.plugin_tree.tag_configure(tag, foreground=color)
        uinput.attach_tree(self.plugin_tree)
        logcard = T.Card(parent, padding=8, tone=T.CARD_SOFT)
        logcard.pack(fill="x")
        self.plugin_log = self._text_box(logcard.inner, height=6, fill=False, font=("Cascadia Mono", 10),
                                         dark=True)

    # -------------------------------------------------------------- инструкция
    def _build_guide(self, parent) -> None:
        bar = self._toolbar(parent)
        self._btn(bar, "Дубликаты", self.start_duplicates)
        self._btn(bar, "Похожие фото", self.start_photo_dupes)
        self._btn(bar, "Битые файлы", self.run_broken)
        self._btn(bar, "Старые форматы: список", lambda: self.run_convert(False))
        self._btn(bar, "Старые: конвертировать", lambda: self.run_convert(True), "good")
        self._btn(bar, "Имена: предложения", lambda: self.run_repair(False))
        self._btn(bar, "Имена: применить", lambda: self.run_repair(True), "danger")
        self._btn(bar, "Оригиналы после конвертации", self.sweep_converted)
        self._note(bar, "Утилиты: кнопка «Оригиналы после конвертации» ищет .doc/.rtf/.odt, рядом с которыми "
                        "уже есть .docx: список ляжет в «Корзину» — оттуда переносите по своему решению.")
        card = T.Card(parent, padding=12)
        card.pack(fill="both", expand=True)
        box = self._text_box(card.inner, height=10, font=("Segoe UI", 11))
        box.insert("1.0", GUIDE_TEXT)
        box.mark_set("insert", "1.0")

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
        top = self._toolbar(parent)
        self._btn(top, "💾 Сохранить настройки", self.save_settings, "accent")
        self._btn(top, "Диагностика", self.run_doctor)
        self._note(top, "страница прокручивается колесом мыши; всё сохранённое действует со следующей партии")
        sections = self._toolbar(parent, pady=(0, 8))
        self._note(sections, "разделы, которых нет во вкладках:")
        for key, title in PAGES:
            if key in HIDDEN_PAGES:
                self._btn(sections, title, lambda k=key: self._show_page(k))

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
                          "D:\\Archive\\Department)")
        tk.Checkbutton(block, text="автовыбор первого существующего корня из списка (для разных ПК)",
                       bg=T.CARD, fg=T.INK, activebackground=T.CARD, selectcolor=T.CARD_SOFT,
                       anchor="w", font=("Segoe UI", 10),
                       variable=self.svars["auto_pick"]).pack(fill="x", pady=(2, 4))
        self._field_with_browse(block, "Папка результатов", self.svars["output_dir"])
        self._field_with_browse(block, "Папка корзины", self.svars["quarantine"])

        self._build_models_card(parent)

        block = self._card(parent, "🤖 Модель и контекст")
        self._entry_field(parent=block, label="Адрес локальной модели Ollama (нужен только при включённой модели)",
                          variable=self.svars["base_url"],
                          note="по умолчанию http://127.0.0.1:11434. Нет видеокарты здесь — "
                               "впишите http://адрес-компьютера-с-картой:11434, и модель будет "
                               "думать на нём (там должен быть запущен Ollama с OLLAMA_HOST=0.0.0.0)")
        self._model_combos = [
            (self._combo_field(block, "Текстовая модель", self.svars["model"], self._model_options()),
             self.svars["model"]),
            (self._combo_field(block, "Модель для зрения", self.svars["vision_model"], self._model_options()),
             self.svars["vision_model"]),
        ]
        self.fetch_model_names_async()
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
        self._entry_field(block, "Модель эмбеддингов Ollama (поиск папок по смыслу; пусто = выкл; нужна только при включённой модели)",
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

        buttons = self._toolbar(parent, pady=(4, 10))
        self._btn(buttons, "💾 Сохранить настройки", self.save_settings, "accent")
        self._btn(buttons, "Диагностика", self.run_doctor)
        self._btn(buttons, "Открыть config.json", lambda: open_path(self.config["config_path"]))
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

    def _combo_field(self, parent, label: str, variable, values, readonly: bool = False, note: str = "") -> ttk.Combobox:
        from ui import i18n as _I
        label = _I.tr(label)
        row = tk.Frame(parent, bg=T.CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=T.CARD, fg=T.MUTED, width=26, anchor="w",
                 font=("Segoe UI", 10)).pack(side="left")
        combo = ttk.Combobox(row, textvariable=variable, values=list(values), width=28,
                             state="readonly" if readonly else "normal")
        combo.pack(side="left")
        if note:
            tk.Label(row, text="  " + note, bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9),
                     wraplength=560, justify="left").pack(side="left", padx=8)
        return combo

    # ------------------------------------------------------------------ журнал
    def _build_log(self, parent) -> None:
        bar = self._toolbar(parent)
        self._btn(bar, "📋 Копировать всё", self.copy_log, "accent")
        self._btn(bar, "Очистить экран", lambda: self.log_text.delete("1.0", "end"))
        self._btn(bar, "Журнал операций", lambda: self._open_name("operations_log.csv"))
        self._btn(bar, "История переносов", lambda: self._open_name(mapio.APPLIED_NAME))
        self._btn(bar, "Проблемные файлы", lambda: self._open_name("broken_files.csv"))
        card = T.Card(parent, padding=8)
        card.pack(fill="both", expand=True)
        self.log_text = self._text_box(card.inner, height=12, font=("Cascadia Mono", 10), dark=True)
        for tag, color in (("error", "#fda29b"), ("ok", "#86efac"), ("note", "#93c5fd")):
            self.log_text.tag_configure(tag, foreground=color)

    def copy_log(self) -> None:
        text = self.log_text.get("1.0", "end-1c")
        if text.strip():
            uinput.put_on_clipboard(self.log_text, text)
            self.status_var.set("журнал скопирован в буфер обмена")

    # ------------------------------------------------------------------- общее
    def _model_names(self) -> tuple[str, ...]:
        info = getattr(self, "_model_info", None) or {}
        names = [r["name"] for r in info.get("rows", [])]
        return tuple(sorted(set(names) | {self.config["llm"]["model_text"], self.config["llm"]["model_vision"]}))

    def _model_options(self) -> tuple[str, ...]:
        """Список для выпадашек БЕЗ сетевого запроса: настройки + прошлый ответ Ollama."""
        info = getattr(self, "_model_info", None) or {}
        cached = [r["name"] for r in info.get("rows", []) if not r.get("embed")]
        return tuple(sorted(set(cached) | {self.config["llm"]["model_text"],
                                           self.config["llm"]["model_vision"]}))

    def _build_models_card(self, parent) -> None:
        card = self._card(parent, "🧠 Локальная модель (Ollama) — необязательно")
        self.models_status = tk.Label(card, text="Проверяю Ollama…", bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10),
                                      justify="left", anchor="w", wraplength=900)
        self.models_status.pack(anchor="w", fill="x")
        columns = ("name", "size", "caps", "role")
        holder = tk.Frame(card, bg=T.CARD)
        holder.pack(fill="x", pady=(6, 4))
        self.models_tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode="browse", height=4)
        for col, head, width, stretch in (("name", "Модель", 260, True), ("size", "Размер", 80, False),
                                          ("caps", "Умеет", 200, True), ("role", "Сейчас", 220, True)):
            self.models_tree.heading(col, text=head)
            self.models_tree.column(col, width=width, minwidth=60, stretch=stretch)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.models_tree.yview)
        self.models_tree.configure(yscrollcommand=bar.set)
        self.models_tree.pack(side="left", fill="x", expand=True)
        bar.pack(side="right", fill="y")
        uinput.attach_tree(self.models_tree, extra=[
            ("Копировать имя модели", lambda: uinput.put_on_clipboard(
                self.models_tree, "\n".join(str(self.models_tree.set(i, "name"))
                                              for i in self.models_tree.selection()))),
        ])
        buttons = T.Flow(card, bg=T.CARD)
        buttons.pack(fill="x", pady=(4, 0))
        for label, command, style in (
                ("🔄 Проверить локальную модель", self.fetch_model_names_async, "ghost"),
                ("Взять как текстовую", lambda: self._use_selected_model("model"), "ghost"),
                ("Взять для зрения", lambda: self._use_selected_model("vision_model"), "ghost"),
                ("✨ Подобрать лучшие", self.auto_pick_models, "good"),
                ("⬇ Скачать модель…", self.open_model_downloader, "accent")):
            pill = T.Pill(buttons, label, command=command, style=style, bg=T.CARD)
            buttons.add(pill)

    def _render_models(self) -> None:
        """Заполнить таблицу моделей и строку состояния по последнему ответу Ollama."""
        if not hasattr(self, "models_tree"):
            return
        info = getattr(self, "_model_info", None) or {}
        self.models_tree.delete(*self.models_tree.get_children())
        if not info.get("alive"):
            self.models_status.configure(
                fg=T.BAD, text="Локальная модель не отвечает. Запустите Ollama (или проверьте адрес выше). Если модель не нужна, выключите её кнопкой в шапке. "
                               "и нажмите «Проверить Ollama».")
            return
        from core.llm import same_model
        want_text = self.svars["model"].get().strip()
        want_vision = self.svars["vision_model"].get().strip()
        for row in info["rows"]:
            roles = []
            if same_model(row["name"], want_text):
                roles.append("текст")
            if same_model(row["name"], want_vision) and self.svars["vision"].get():
                roles.append("зрение")
            if row.get("embed"):
                roles.append("эмбеддинги")
            caps = ", ".join(c for c in row["caps"] if c != "completion") or ("текст" if row["caps"] else "?")
            self.models_tree.insert("", "end", iid=row["name"], values=(
                row["name"], f"{row['gb']:.1f} ГБ", caps, " + ".join(roles)))
        missing = [w for w in (want_text, want_vision if self.svars["vision"].get() else "") if w
                   and not any(same_model(w, r["name"]) for r in info["rows"])]
        tail = ""
        if missing:
            tail = ("  ⚠ не установлено: " + ", ".join(missing) +
                    (f". Программа возьмёт «{info.get('best_text')}»." if info.get("best_text") else "") +
                    " Скачать выбранную можно кнопкой справа.")
        self.models_status.configure(
            fg=T.WARN if missing else T.GOOD,
            text=f"Ollama {info.get('version') or ''} · моделей: {len(info['rows'])}.{tail}")

    def _use_selected_model(self, var: str) -> None:
        picked = self.models_tree.selection()
        if not picked:
            messagebox.showinfo("Модели", "Сначала выберите модель в таблице.", parent=self)
            return
        self.svars[var].set(picked[0])
        self._render_models()
        self.status_var.set(f"выбрано: {picked[0]} — нажмите «Сохранить настройки»")

    def auto_pick_models(self) -> None:
        info = getattr(self, "_model_info", None) or {}
        if not info.get("alive") or not info.get("rows"):
            messagebox.showinfo("Модели", "Локальная модель не отвечает или в ней нет моделей. Скачайте модель кнопкой "
                                          "«Скачать модель…».", parent=self)
            return
        text, vision = info.get("best_text"), info.get("best_vision")
        if text:
            self.svars["model"].set(text)
        if vision:
            self.svars["vision_model"].set(vision)
        self._render_models()
        self.status_var.set(f"подобрано: текст — {text or '—'}, зрение — {vision or 'нет подходящей'}; "
                            "нажмите «Сохранить настройки»")

    def open_model_downloader(self) -> None:
        from core import models as models_mod
        win = tk.Toplevel(self)
        win.title("Скачать модель")
        win.geometry("760x420")
        win.configure(bg=T.CARD)
        tk.Label(win, bg=T.CARD, fg=T.INK, font=("Segoe UI Semibold", 12), justify="left", anchor="w",
                 text="Выберите модель — программа скачает её через Ollama (нужен интернет, "
                      "файлы большие).").pack(fill="x", padx=14, pady=(12, 4))
        tree = ttk.Treeview(win, columns=("tag", "gb", "role", "why"), show="headings", selectmode="browse", height=7)
        for col, head, width in (("tag", "Модель", 130), ("gb", "ГБ", 50), ("role", "Для чего", 190),
                                 ("why", "Железо и особенности", 330)):
            tree.heading(col, text=head)
            tree.column(col, width=width, stretch=col in {"role", "why"})
        installed = [r["name"] for r in (getattr(self, "_model_info", None) or {}).get("rows", [])]
        for rec in models_mod.RECOMMENDED:
            have = any(models_mod.same_model(rec["tag"], n) for n in installed)
            tree.insert("", "end", iid=rec["tag"], values=(rec["tag"] + (" ✓" if have else ""), rec["gb"],
                                                           rec["role"], rec["why"]))
        tree.pack(fill="both", expand=True, padx=14, pady=4)
        uinput.attach_tree(tree)
        custom = tk.StringVar()
        row = tk.Frame(win, bg=T.CARD)
        row.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(row, text="или имя модели:", bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left")
        tk.Entry(row, textvariable=custom, bg=T.CARD_SOFT, fg=T.INK, relief="flat", highlightthickness=1,
                 highlightbackground=T.BORDER, highlightcolor=T.ACCENT, font=("Segoe UI", 11)
                 ).pack(side="left", fill="x", expand=True, padx=8, ipady=4)

        def start() -> None:
            tag = custom.get().strip() or (tree.selection()[0] if tree.selection() else "")
            if not tag:
                messagebox.showinfo("Скачать модель", "Выберите модель в списке или впишите её имя.", parent=win)
                return
            win.destroy()
            self.pull_model(tag)

        bar = tk.Frame(win, bg=T.CARD)
        bar.pack(fill="x", padx=14, pady=10)
        T.Pill(bar, "⬇ Скачать", command=start, style="accent", bg=T.CARD).pack(side="left")
        T.Pill(bar, "Закрыть", command=win.destroy, style="quiet", bg=T.CARD).pack(side="left", padx=6)

    def pull_model(self, tag: str) -> None:
        from core.llm import client_from_config

        def work():
            client = client_from_config(self.config, "text")
            last = {"pct": -1}

            def progress(status: str, done: int, total: int) -> None:
                pct = int(done * 100 / total) if total else 0
                if total and pct == last["pct"]:
                    return
                last["pct"] = pct
                self.events.put(("progress", pct, 100, f"Скачивание {tag} ({status[:24]})"))

            client.pull(tag, progress)
            self.events.put(("models_refresh",))
            return {"модель": tag}

        self.log_event(f"скачивание модели {tag}…")
        self.run_worker(f"Скачивание {tag}", work)

    def fetch_model_names_async(self) -> None:
        """Список моделей приезжает фоном — окно не виснет при незапущенной Ollama."""
        def work() -> None:
            try:
                info = self._collect_model_info()
            except Exception:
                info = {"alive": False, "version": "", "rows": [], "best_text": "", "best_vision": ""}
            self.events.put(("models", info))

        threading.Thread(target=work, daemon=True, name="models").start()

    def _collect_model_info(self) -> dict:
        """Что установлено в Ollama и что подойдёт — для блока «Модели» и выпадающих списков."""
        from core import models as models_mod
        from core.llm import client_from_config

        client = client_from_config(self.config, "text")
        if not client.is_alive():
            return {"alive": False, "version": "", "rows": [], "best_text": "", "best_vision": ""}
        details = client.list_models_detailed()
        rows = []
        for item in details:
            name = item["name"]
            caps = client.capabilities(name)
            rows.append({"name": name, "gb": int(item.get("size") or 0) / 1e9, "caps": caps,
                         "embed": models_mod.is_embedding(name)})
        return {"alive": True, "version": client.version(), "rows": rows,
                "best_text": models_mod.pick_best(details, vision=False, client=client),
                "best_vision": models_mod.pick_best(details, vision=True, client=client)}

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
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event)
                except Exception:  # одно сбойное событие не должно убивать насос UI
                    try:
                        self.append_log(traceback.format_exc(), "error")
                    except Exception:
                        pass
        finally:
            try:
                self.after(90, self._drain)
            except tk.TclError:
                pass   # окно закрывается — насос больше не нужен

    def _handle_event(self, event: tuple) -> None:
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
            self._model_info = event[1] if isinstance(event[1], dict) else getattr(self, "_model_info", None)
            self._show_ollama_state(event[1])
        elif kind == "models":
            self._model_info = event[1]
            for combo, var in getattr(self, "_model_combos", []):
                try:
                    combo.configure(values=list(self._model_options()))
                except tk.TclError:
                    pass   # страница настроек уже закрыта
            self._render_models()
            self._show_ollama_state(event[1])
        elif kind == "models_refresh":
            self.fetch_model_names_async()
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
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Занято", "Разбор уже идёт — дождитесь или нажмите «Стоп».")
            return
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
            rows, added, updated, skipped = mapio.merge_suggestions(
                rows, records, manual=mapio.load_manual(self.config["output_path"]))
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

    def _text_window(self, title: str, text: str, size: str = "980x640", wrap: str = "word") -> tk.Toplevel:
        return text_window(self, title, text, size=size, wrap=wrap,
                           on_copy=lambda: self.status_var.set("текст скопирован в буфер обмена"))

    def show_dry_run(self) -> None:
        self.save_map(silent=True)
        plan = applier.build_plan(self.config, self.rows)
        lines, counters = applier.describe(plan, self.config)
        head = (f"Файлов: {counters['всего']} · Объём: {fmt_size(counters['байт'])} · "
                f"Новых папок: {counters['новых папок']} · Конфликтов имён: {counters['конфликтов']} · "
                "удалений нет")
        self._text_window("Пробный перенос — файлы не изменяются",
                          head + "\n\n" + ("\n".join(lines) if lines else "Нет отмеченных действий."),
                          size="1060x660", wrap="none")

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

    def trash_selected_map_rows(self) -> None:
        """Выделенные строки карты — в корзину отдела. Файлы только перемещаются, строки уходят из карты."""
        rows = [row for row in self.selected_rows() if row.get("Статус") != "перенесено"]
        if not rows:
            messagebox.showinfo("В корзину", "Выделите строки в карте: клик, а для нескольких — Shift или Ctrl.")
            return
        self.save_map(silent=True)   # карта на диске должна быть свежей до переноса: перенос сам уберёт из неё строки
        marked = [{"Путь": str(row.get("Текущий путь", "")), "Отметить": "TRUE",
                   "Причина": str(row.get("Причина", ""))} for row in rows]
        self.run_worker("В корзину", lambda: cleanup.move_to_trash(
            self.config, marked, progress=lambda i, n, rel: self.events.put(("progress", i, n, "Корзина"))))

    def set_apply(self, value: bool) -> None:
        rows = self.selected_rows()
        if value and any(row.get("Статус") == "проверка" for row in rows):
            if not messagebox.askyesno("Отметка проверочной строки",
                                       "Среди выбранных есть «проверка». Отметить и их?"):
                rows = [row for row in rows if row.get("Статус") != "проверка"]
        for row in rows:
            if row.get("Статус") != "перенесено":
                if not value and (row.get("Применить") or "").upper() == "TRUE":
                    mapio.mark_manual(self.config["output_path"], str(row.get("Текущий путь", "")))   # человек снял отметку
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
            if any(str(updated.get(k, "")).strip() != str(row.get(k, "")).strip()
                   for k in ("Новое имя", "Целевой путь", "Путь ярлыка", "Комментарий")):
                mapio.mark_manual(self.config["output_path"], str(row.get("Текущий путь", "")))   # правка руками
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
        self._set_ro_text(
            self.trash_path_box,
            f"{row.get('Путь')}\nразмер {fmt_size(int(row.get('Размер') or 0))} · изменён "
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
        try:
            reveal_path(full)
        except Exception as exc:
            messagebox.showerror("Показать в папке", str(exc))

    def open_trash_file(self) -> None:
        """Открыть выбранный файл программой по умолчанию, чтобы проверить перед решением."""
        full = self._trash_full_path()
        if not full:
            return
        try:
            open_path(full)
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
                self.folder_tree.insert(parent, "end", iid=child.path, text=child.name,
                                        values=(child.total,), open=depth < 2)
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
        self._text_window("Что уйдёт в модель", text, size="1040x700")

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
        if llm.get("structured_output", True) and not block:
            lines.append("Схема ответа: структура JSON навязывается Ollama (structured_output), "
                         f"описывать её в тексте инструкции не нужно ({len(required)} обязательных полей).")
        elif not block:
            lines.append("⚠ structured_output выключен, а в инструкции нет раздела «ФОРМАТ ОТВЕТА» — "
                         "модель может отвечать не по схеме. Включите structured_output или опишите формат.")
        else:
            missing = [name for name in required if name not in block]
            extra = [name for name in RESPONSE_SCHEMA["properties"] if name not in block]
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
        self._text_window(title, text, size="900x560")

    # ----------------------------------------------------------------- корзина
    def load_trash(self, silent: bool = False) -> None:
        self.trash_path_label.configure(text="Папка корзины: " + str(cleanup.trash_dir(self.config)))
        if not self.config.get("plugins", {}).get("quarantine", True):
            self.trash_rows = []
        else:
            try:
                records = mapio.load_records(self.config["output_path"] / SUGGESTIONS_NAME)
                self.trash_rows = cleanup.candidates(self.config) + cleanup.mark_from_model(records)
                known = {row["Путь"] for row in self.trash_rows}
                self.trash_rows += [row for row in cleanup.from_map(self.config) if row["Путь"] not in known]
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
        if getattr(self, "_current_page", "") == "trash":
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
        self._text_window("Диагностика", "\n".join(lines) + f"\n\nкод возврата: {code}", size="900x560")

    def _ollama_enabled(self) -> bool:
        return bool(self.config.get("llm", {}).get("enabled", True))

    def toggle_ollama(self) -> None:
        """Выключатель локальной модели: выключенная Ollama не запускается и не нагружает ПК."""
        self.config["llm"]["enabled"] = not self._ollama_enabled()
        save_config(self.config)
        self.check_ollama()

    def check_ollama(self) -> None:
        """Статус Ollama в шапке: жива ли, есть ли выбранная модель, чем её заменят."""
        if not self._ollama_enabled():
            self.ollama_label.config(text="Ollama выключена: не используется, ресурсы ПК не тратятся", fg=T.MUTED)
            if hasattr(self, "ollama_toggle"):
                self.ollama_toggle.configure_text("Ollama: включить")
            return
        if hasattr(self, "ollama_toggle"):
            self.ollama_toggle.configure_text("Ollama: выключить")

        def worker() -> None:
            try:
                info = self._collect_model_info()
            except Exception:
                info = {"alive": False, "version": "", "rows": [], "best_text": "", "best_vision": ""}
            self.events.put(("models", info))

        threading.Thread(target=worker, daemon=True, name="ollama-check").start()

    def _show_ollama_state(self, info) -> None:
        from core.llm import same_model
        info = info if isinstance(info, dict) else {}
        want = self.config["llm"]["model_text"]
        if not info.get("alive"):
            self.set_dot(T.BAD)
            self.ollama_label.configure(text="Ollama · не отвечает")
            return
        have = any(same_model(want, r["name"]) for r in info.get("rows", []))
        if have:
            self.set_dot(T.GOOD)
            self.ollama_label.configure(text=f"Ollama · {want} готова")
        elif info.get("best_text"):
            self.set_dot(T.WARN)
            self.ollama_label.configure(text=f"Ollama · нет {want}, возьму {info['best_text']}")
        else:
            self.set_dot(T.WARN)
            self.ollama_label.configure(text=f"Ollama · нет модели {want}")

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    "Работа идёт",
                    "Остановить после текущего файла и закрыть, ДОЖДАВШИСЬ её конца?\n"
                    "Закрывать посреди перемещения файлов опасно — не закрывайте окно кнопкой «Нет»."):
                return
            self.stop_work()
            self.status_var.set("закрою после завершения текущей операции…")
            self._close_when_worker_done()
            return
        self.destroy()

    def _close_when_worker_done(self) -> None:
        if self.worker and self.worker.is_alive():
            self.after(400, self._close_when_worker_done)
        else:
            self.destroy()

"""Окно просмотра файла: прочитать содержимое глазами и утвердить предложение.

Содержимое занимает бо́льшую часть окна; картинки и PDF-страницы масштабируются:
«по окну» по умолчанию, кнопки − / % / +, Ctrl+колесо — зум, двойной клик —
сброс. Текст выделяется и копируется (Ctrl+C или правая кнопка). Справа — только
дело: было / станет / решение. «🔬 Перепроверить глубже» отдаёт файл модели
повторно с расширенным контекстом и показывает улучшенный вариант карточкой.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.extract import IMAGE_EXT, extract
from ui import theme as T


def reveal_in_explorer(path: Path) -> None:
    if not Path(path).exists():
        messagebox.showwarning("Нет файла", "Файла нет на диске — возможно, он уже перенесён.")
        return
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


class FileViewer(tk.Toplevel):
    """Просмотр одного файла карты с правкой, зумом, перепроверкой и утверждением."""

    DECISIONS = ("Утвердить", "Проверка", "Не трогать", "Отклонить")
    DECISION_KEYS = ("ready", "review", "keep", "reject")
    DECISION_HINTS = {
        "ready": "«готово», строка отмечена к переносу",
        "review": "решение позже — в перенос не пойдёт",
        "keep": "файл уже в порядке — уйдёт в «Историю» без переноса",
        "reject": "модель ошиблась — оставляем как есть в рабочей карте",
    }

    def __init__(self, master, config: dict, row: dict, on_save=None, on_apply=None):
        super().__init__(master)
        self.config = config
        self.row = dict(row)
        self.on_save = on_save
        self.on_apply = on_apply
        self.root_dir: Path = config["scan_root"]
        self.path = self.root_dir / self.row.get("Текущий путь", "")
        rel = self.row.get("Текущий путь", "")
        self.title(f"Просмотр · {Path(str(rel)).name}")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1280, sw - 40)}x{min(800, sh - 90)}")
        self.minsize(940, 600)
        self.transient(master)
        # разворачиваем на весь экран, когда окно реально появилось: полей много,
        # в 800px по высоте они не вмещаются и окно приходилось тянуть вниз
        self.bind("<Map>", self._zoom_on_map, add="+")
        self._photo = None
        self._pil_img = None            # оригинал PIL для пересчёта зума
        self._zoom = 1.0                # множитель относительно «вписать»
        self._zoom_fit = True
        self._refining = False
        self._refined: dict | None = None
        self._decision = tk.StringVar(value=self._initial_decision())
        self._view_mode = tk.StringVar(value="имя + путь")
        self._set_window_icon()
        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Control-s>", lambda _e: (self.save(), "break")[1])
        self.after(80, self._load_content)

    def _set_window_icon(self) -> None:
        dirs = []
        app_dir = self.config.get("app_dir")
        if app_dir:
            dirs.append(Path(app_dir))
        dirs.append(Path(__file__).resolve().parent.parent)
        for folder in dirs:
            icon = folder / "logo.png"
            if icon.exists():
                try:
                    self.iconphoto(True, tk.PhotoImage(file=icon))
                    break
                except tk.TclError:
                    continue

    def _initial_decision(self) -> str:
        status = (self.row.get("Статус") or "").strip()
        if status == "без изменений":
            return "keep"
        if status == "отказ":
            return "reject"
        if status == "готово" or (self.row.get("Применить") or "").upper() == "TRUE":
            return "ready"
        return "review"

    def _zoom_on_map(self, _event=None) -> None:
        """Развернуть окно на всю рабочую область экрана один раз, когда оно появилось.

        На Windows state('zoomed') растягивает окно поверх панели задач, и нижняя
        кнопка «Сохранить» уходит под таскбар. Поэтому берём рабочую область
        (экран минус панель задач) и ставим окно ровно в неё.
        """
        if getattr(self, "_zoomed_once", False):
            return
        self._zoomed_once = True
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                rect = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                    aw, ah = rect.right - rect.left, rect.bottom - rect.top
                    if aw > 400 and ah > 300:
                        self.geometry(f"{aw}x{ah}+{rect.left}+{rect.top}")
                        self.update_idletasks()
                        deco = max(0, self.winfo_rooty() - rect.top)   # высота заголовка окна
                        self.geometry(f"{aw}x{max(300, ah - deco)}+{rect.left}+{rect.top}")
                        return
            except Exception:  # noqa: BLE001
                pass
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        bg = T.Gradient(self)
        bg.place(x=0, y=0, relwidth=1, relheight=1)

        head = tk.Frame(self, bg=T.BG_TOP)
        head.pack(side="top", fill="x", padx=18, pady=(12, 0))
        path_lbl = tk.Label(head, text=self.row.get("Текущий путь", ""), bg=T.BG_TOP, fg=T.INK,
                            font=("Segoe UI Semibold", 14), anchor="w", justify="left",
                            wraplength=1240)
        path_lbl.pack(fill="x")
        path_lbl.bind("<Configure>", lambda e: path_lbl.configure(wraplength=max(300, e.width)))
        meta = []
        if self.path.exists():
            meta.append(f"размер {self.path.stat().st_size / 1024:.0f} КБ")
        for label, key in (("имя", "Уверенность имени"), ("папка", "Уверенность переноса"),
                           ("статус", "Статус"), ("действие", "Тип операции")):
            value = str(self.row.get(key, "")).strip()
            if value:
                meta.append(f"{label}: {value}")
        tk.Label(head, text="  ·  ".join(meta), bg=T.BG_TOP, fg=T.MUTED,
                 font=("Segoe UI", 11)).pack(anchor="w", pady=(3, 0))
        reason = str(self.row.get("Причина", ""))
        if reason:
            wrapped = tk.Label(head, text=reason, bg=T.BG_TOP, fg=T.WARN, wraplength=1180,
                               justify="left", anchor="w", font=("Segoe UI", 10))
            wrapped.pack(fill="x", pady=(3, 0))
            wrapped.bind("<Configure>", lambda e: wrapped.configure(wraplength=max(400, e.width)))

        # нижняя панель кнопок крепим ДО body, иначе body с expand=True заберёт
        # всё место и кнопки перекроются (их содержимое добавим ниже)
        buttons = tk.Frame(self, bg=T.BG_TOP)
        buttons.pack(side="bottom", fill="x", padx=18, pady=(0, 12))

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(side="top", fill="both", expand=True, padx=18, pady=(8, 8))

        # ------------------------------------------------- левая колонка: содержимое
        left_card = T.Card(body, padding=10, fill=True)
        body.add(left_card, weight=7)
        inner = left_card.inner
        inner.pack(fill="both", expand=True)
        cap = tk.Frame(inner, bg=T.CARD)
        cap.pack(fill="x", pady=(0, 6))
        tk.Label(cap, text="📄 Содержимое", bg=T.CARD, fg=T.MUTED,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        self.zoom_bar = tk.Frame(cap, bg=T.CARD)
        self.zoom_bar.pack(side="right")
        for label, cmd in (("−", lambda: self._zoom_step(-1)), ("⤢ вписать", self._zoom_reset),
                           ("+", lambda: self._zoom_step(1))):
            T.Pill(self.zoom_bar, label, command=cmd, style="ghost", width=70).pack(side="left", padx=2)
        self.zoom_pct = tk.Label(self.zoom_bar, text="", bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 10))
        self.zoom_pct.pack(side="left", padx=6)

        self._left = inner
        self.preview = tk.Text(inner, wrap="word", font=("Cascadia Mono", 11), bg=T.CARD_SOFT,
                               relief="flat", padx=14, pady=12, fg=T.INK, spacing1=2,
                               insertbackground=T.INK)
        self.preview.pack(fill="both", expand=True)
        self.preview.insert("1.0", "читаю файл…")
        self.preview.configure(state="disabled")
        T.selectable_copy(self.preview)
        self.canvas = tk.Canvas(inner, bg=T.CARD_SOFT, highlightthickness=0)
        self.canvas_scroll = ttk.Scrollbar(inner, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.canvas_scroll.set)
        self.image_label = tk.Label(self.canvas, anchor="n", background=T.CARD_SOFT)
        self.canvas.create_window((0, 0), window=self.image_label, anchor="nw")
        self.image_label.bind("<Configure>",
                              lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<MouseWheel>", self._canvas_wheel)
        self.canvas.bind("<Double-1>", lambda _e: self._zoom_reset())

        # ------------------------------------------------ правая колонка: решение
        right_card = T.Card(body, padding=10, fill=True)
        body.add(right_card, weight=3)
        right_card.inner.pack(fill="both")
        panel = T.Scrollable(right_card.inner, bg=T.CARD)
        panel.pack(fill="both", expand=True)
        panel.grab_wheel(True)
        self._panel = panel
        p = panel.body
        p.pack(fill="x")

        def section(text: str) -> None:
            tk.Label(p, text=text, bg=T.CARD, fg=T.MUTED,
                     font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(12, 2))

        def entry(parent, variable) -> ttk.Entry:
            field = ttk.Entry(parent, textvariable=variable)
            field.pack(fill="x", ipady=3)
            return field

        section("БЫЛО")
        was = tk.Text(p, height=2, wrap="word", bg=T.CARD_SOFT, relief="flat", padx=8, pady=6,
                      font=("Segoe UI", 10), fg=T.INK)
        was.insert("1.0", str(self.row.get("Текущий путь", "")))
        was.pack(fill="x")
        T.selectable_copy(was)

        section("НОВОЕ ИМЯ")
        self.name_var = tk.StringVar(value=self.row.get("Новое имя", ""))
        entry(p, self.name_var)

        def folder_field(variable) -> ttk.Entry:
            holder = tk.Frame(p, bg=T.CARD)
            holder.pack(fill="x", pady=(0, 6))
            T.Pill(holder, "📁", command=lambda: self._pick_folder(variable),
                   style="ghost", width=64).pack(side="right", padx=(8, 0))
            field = ttk.Entry(holder, textvariable=variable)
            field.pack(side="left", fill="x", expand=True, ipady=3)
            return field

        section("ЦЕЛЕВАЯ ПАПКА")
        self.target_var = tk.StringVar(value=self.row.get("Целевой путь", ""))
        folder_field(self.target_var)

        section("ЯРЛЫК В (необязательно)")
        self.link_var = tk.StringVar(value=self.row.get("Путь ярлыка", ""))
        folder_field(self.link_var)

        seg = T.Segmented(p, ("Имя", "Имя+путь", "Путь"), command=self._mode_switched, bg=T.CARD)
        seg.select(1, slide=False)
        seg.pack(fill="x", ipady=2, pady=(12, 0))
        self.final_line = tk.Label(p, text="", bg=T.CARD, fg=T.ACCENT, font=("Segoe UI Semibold", 10),
                                   wraplength=330, justify="left")
        self.final_line.pack(anchor="w", pady=(4, 0))
        self.name_var.trace_add("write", lambda *_: self._refresh_final())
        self.target_var.trace_add("write", lambda *_: self._refresh_final())
        self._refresh_final()

        section("РЕШЕНИЕ")
        self._decisions_seg = T.Segmented(p, self.DECISIONS, command=self._decision_switched, bg=T.CARD)
        self._decisions_seg.select(self.DECISION_KEYS.index(self._decision.get()), slide=False)
        self._decisions_seg.pack(fill="x", ipady=2)
        self.decision_hint = tk.Label(p, text=self.DECISION_HINTS[self._decision.get()],
                                      bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9),
                                      wraplength=330, justify="left")
        self.decision_hint.pack(anchor="w", pady=(2, 0))

        section("КОММЕНТАРИЙ")
        self.comment = tk.Text(p, height=3, wrap="word", bg=T.CARD_SOFT, relief="flat",
                               padx=8, pady=6, font=("Segoe UI", 10), fg=T.INK,
                               insertbackground=T.INK)
        self.comment.insert("1.0", self.row.get("Комментарий", ""))
        self.comment.pack(fill="x")

        self.refine_box = tk.Frame(p, bg=T.CARD_SOFT, highlightthickness=1,
                                   highlightbackground=T.BORDER)
        self.refine_label = tk.Text(self.refine_box, height=6, wrap="word", bg=T.CARD_SOFT,
                                    relief="flat", padx=10, pady=8, fg=T.MUTED, font=("Segoe UI", 10),
                                    insertbackground=T.INK, cursor="xterm")
        self.refine_label.pack(fill="x", padx=4, pady=(6, 2))
        T.selectable_copy(self.refine_label)

        def _fit_right_wraps(event=None) -> None:
            width = max(200, (event.width if event else p.winfo_width()) - 6)
            for lbl in (self.final_line, self.decision_hint):
                try:
                    lbl.configure(wraplength=width)
                except tk.TclError:
                    pass
        p.bind("<Configure>", _fit_right_wraps)
        self.after(150, _fit_right_wraps)

        T.Pill(buttons, "Открыть в проводнике", command=lambda: reveal_in_explorer(self.path),
               style="ghost", bg=T.BG_TOP).pack(side="left")
        T.Pill(buttons, "Показать промпт", command=self.show_prompt, style="ghost",
               bg=T.BG_TOP).pack(side="left", padx=6)
        self.refine_pill = T.Pill(buttons, "🔬 Перепроверить глубже", command=self.refine_now,
                                  style="good", bg=T.BG_TOP)
        self.refine_pill.pack(side="left", padx=6)
        T.Pill(buttons, "Отмена", command=self.destroy, style="quiet", bg=T.BG_TOP).pack(side="right")
        T.Pill(buttons, "Сохранить и закрыть", command=self.save, style="accent",
               bg=T.BG_TOP).pack(side="right", padx=6)
        T.Pill(buttons, "📦 Применить сейчас", command=self.apply_now, style="good",
               bg=T.BG_TOP).pack(side="right", padx=6)

        # Перенос текста в правой колонке под фактическую ширину панели — чтобы при
        # узком окне ничего не уезжало за край; плюс стартовое разделение колонок.
        def _sync_wrap(_e=None) -> None:
            w = max(200, p.winfo_width() - 6)
            for lab in (self.final_line, self.decision_hint, self.refine_label):
                try:
                    lab.configure(wraplength=w)
                except tk.TclError:
                    pass

        p.bind("<Configure>", _sync_wrap)

        def _init_sash() -> None:
            try:
                total = body.winfo_width()
                if total > 2:
                    body.sashpos(0, max(320, total - 430))
            except tk.TclError:
                pass

        self.after(150, _init_sash)

    # ---------------------------------------------------------------- helpers
    def _mode_switched(self, index: int, _label: str) -> None:
        self._view_mode.set(("имя", "имя + путь", "путь")[index])
        self._refresh_final()

    def _decision_switched(self, index: int, _label: str) -> None:
        self._decision.set(self.DECISION_KEYS[index])
        self.decision_hint.configure(text=self.DECISION_HINTS[self._decision.get()])

    def _refresh_final(self) -> None:
        name = self.name_var.get().strip() or Path(str(self.row.get("Текущий путь", ""))).name
        target = self.target_var.get().strip().strip("/")
        mode = self._view_mode.get()
        if mode == "имя":
            text = f"Итог: {name}"
        elif mode == "путь":
            text = f"Итог: {target or '— папка не выбрана —'}"
        else:
            text = f"Итог: {target + '/' if target else ''}{name}"
        self.final_line.configure(text=text)

    def _pick_folder(self, variable: tk.StringVar) -> None:
        """Выбрать папку в проводнике; путь от корня отдела — относительным."""
        initial = self.root_dir
        current = variable.get().strip().strip("/")
        if current:
            candidate = self.root_dir / current
            if candidate.is_dir():
                initial = candidate
        chosen = filedialog.askdirectory(title="Выберите папку", initialdir=str(initial), parent=self)
        if not chosen:
            return
        path = Path(chosen)
        try:
            variable.set(str(path.relative_to(self.root_dir)).replace("\\", "/"))
        except ValueError:
            messagebox.showinfo("Вне диска отдела",
                                f"Папка {path} вне корня отдела — карта работает с путями от корня.\n"
                                "Можно вставить путь вручную.", parent=self)

    # ---------------------------------------------------------------- content
    def _load_content(self) -> None:
        if not self.path.exists():
            self._show_text("(файла нет на диске — возможно, он уже перенесён вручную)")
            return
        data = extract(self.path, 12000, 4)
        ext = self.path.suffix.lower()
        if ext in IMAGE_EXT:
            self._show_image(data)
        elif ext == ".pdf":
            self._show_pdf(data)
        else:
            self._show_text(data.text or f"(текст не извлечён: {data.note or data.kind})")

    def _reset_left(self) -> None:
        self.preview.pack_forget()
        self.canvas_scroll.pack_forget()
        self.canvas.pack_forget()
        self.image_label.configure(image="", text="")

    def _show_text(self, text: str) -> None:
        self._pil_img = None
        self.zoom_bar.pack_forget()
        self._reset_left()
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text or "(пусто)")
        self.preview.edit_reset()

    # ------------------------------------------------------------------- zoom
    def _show_pil(self, image) -> None:
        """Показать PIL-изображение с учётом зума; «вписать» — по размеру холста."""
        from PIL import ImageTk

        self._pil_img = image
        self._reset_left()
        self.zoom_bar.pack(side="right")
        self.canvas_scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._render_zoom()

    def _render_zoom(self) -> None:
        if self._pil_img is None:
            return
        from PIL import Image, ImageTk

        view_w = max(320, self.canvas.winfo_width() - 8)
        view_h = max(240, self.canvas.winfo_height() - 8)
        base_w, base_h = self._pil_img.size
        fit = min(view_w / base_w, view_h / base_h, 1.0)
        scale = fit * self._zoom if self._zoom_fit else self._zoom
        w, h = max(1, int(base_w * scale)), max(1, int(base_h * scale))
        resized = self._pil_img.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        self.image_label.configure(image=self._photo, text="")
        self.canvas.itemconfigure("all", width=max(view_w, w))
        self.zoom_pct.configure(text=f"{scale * 100:.0f}%")

    def _zoom_step(self, direction: int) -> None:
        base = 1.0 if not self._zoom_fit else min(
            1.0, max(0.05, self._fit_scale()))
        self._zoom = max(0.1, min(8.0, self._zoom * (1.25 if direction > 0 else 0.8)))
        self._zoom_fit = False
        self._render_zoom()

    def _fit_scale(self) -> float:
        if self._pil_img is None:
            return 1.0
        view_w = max(320, self.canvas.winfo_width() - 8)
        view_h = max(240, self.canvas.winfo_height() - 8)
        base_w, base_h = self._pil_img.size
        return min(view_w / base_w, view_h / base_h, 1.0)

    def _zoom_reset(self) -> None:
        self._zoom = 1.0
        self._zoom_fit = True
        self._render_zoom()

    def _canvas_wheel(self, event) -> str:
        if event.state & 0x0004:                       # Ctrl+колесо — зум
            self._zoom_step(1 if event.delta > 0 else -1)
            return "break"
        self.canvas.yview_scroll(-2 if event.delta > 0 else 2, "units")
        return "break"

    def _canvas_resized(self, event) -> None:
        if self._zoom_fit and self._pil_img is not None:
            self._render_zoom()
        else:
            self.canvas.itemconfigure("all", width=max(event.width, 1))

    def _show_image(self, data) -> None:
        try:
            from PIL import Image

            with Image.open(self.path) as image:
                image.thumbnail((1800, 2400))
                image = image.convert("RGB")
        except Exception as exc:
            self._show_text(f"не удалось показать изображение: {exc}")
            return
        self._zoom = 1.0
        self._zoom_fit = True
        self._show_pil(image)

    def _show_pdf(self, data) -> None:
        try:
            import fitz
            from PIL import Image

            with fitz.open(self.path) as doc:
                page = doc[0]
                pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2))
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except Exception:
            self._show_text(data.text or "(PDF без читаемого текста)")
            return
        self._zoom = 1.0
        self._zoom_fit = True
        self._show_pil(image)
        if data.text.strip():
            note = tk.Label(self._left, text="текстовый слой: " + data.text.strip()[:400],
                            bg=T.CARD, fg=T.MUTED, wraplength=900, justify="left", anchor="w",
                            font=("Segoe UI", 10))
            note.pack(fill="x")

    # ------------------------------------------------------------------ refine
    def _refine_set(self, text: str, fg: str | None = None) -> None:
        """Записать текст в карточку результата перепроверки (текст остаётся копируемым)."""
        try:
            self.refine_label.delete("1.0", "end")
            self.refine_label.insert("1.0", text)
            if fg:
                self.refine_label.configure(fg=fg)
        except tk.TclError:
            pass

    def refine_now(self) -> None:
        if self._refining:
            return
        self._refining = True
        self.refine_pill.set_enabled(False)
        self.refine_pill.configure_text("🔬 думаю в три шага…")
        self.refine_box.pack(fill="x", pady=(10, 0))
        self._refine_set(
            "⏳ Перепроверка: модель читает файл заново в три шага — сначала факты, "
            "затем имя, затем место, сверяясь с соседями по папке и вашим комментарием. "
            "Обычно 1–3 минуты.", fg=T.MUTED)
        snapshot = {"Новое имя": self.name_var.get(), "Целевой путь": self.target_var.get(),
                    "Уверенность": str(self.row.get("Уверенность", "")),
                    "Статус": str(self.row.get("Статус", "")),
                    "Причина": str(self.row.get("Причина", ""))}
        comment = self.comment.get("1.0", "end").strip()
        rel = str(self.row.get("Текущий путь", ""))
        self._refine_queue: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                from core.stages import refine_staged
                self._refine_queue.put(("ok", refine_staged(self.config, rel, snapshot, comment)))
            except Exception as exc:
                self._refine_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_refine()

    def _poll_refine(self) -> None:
        try:
            kind, payload = self._refine_queue.get_nowait()
        except queue.Empty:
            self.after(200, self._poll_refine)
            return
        self._refine_done(payload if kind == "ok" else None,
                          None if kind == "ok" else payload)

    def _refine_done(self, answer: dict | None, error: Exception | None) -> None:
        self._refining = False
        self.refine_pill.set_enabled(True)
        self.refine_pill.configure_text("🔬 Перепроверить глубже")
        for child in self.refine_box.winfo_children():
            if getattr(child, "_apply_pill", False):
                child.destroy()
        if error is not None:
            self._refine_set(f"✖ Перепроверка не удалась: {error}", fg=T.BAD)
            messagebox.showerror("Перепроверка",
                                 f"Модель не ответила:\n{type(error).__name__}: {error}\n\n"
                                 "Проверьте, что Ollama запущена (зелёная точка вверху окна программы).",
                                 parent=self)
            return
        self._refined = answer
        name = answer.get("new_name", "")
        target = answer.get("target_path", "")
        reason = answer.get("reason", "")
        overall = float(answer.get("confidence") or 0)
        name_conf = float(answer.get("name_confidence") if answer.get("name_confidence") is not None else overall)
        move_conf = float(answer.get("move_confidence") if answer.get("move_confidence") is not None else overall)
        prev_name = self.name_var.get().strip() or Path(str(self.row.get("Текущий путь"))).name
        prev_target = self.target_var.get().strip().strip("/")
        same = (name.casefold() == prev_name.casefold()
                and target.strip("/").casefold() == prev_target.casefold())
        # сразу подставляем улучшенный вариант в поля — пользователю не нужно ничего искать
        self._apply_refined(quiet=True)
        head = ("✔ УЛУЧШЕННЫЙ ВАРИАНТ подставлен в поля" + (" (совпал с первым)" if same else "")
                + f" — {answer.get('seconds')} с · имя {name_conf:.2f} · папка {move_conf:.2f}")
        lines = [head, f"имя:   {name}", f"папка: {target or '—'}"]
        if reason:
            lines.append(f"почему: {reason[:400]}")
        lines.append("Проверьте поля выше и утвердите. Кнопка ниже вернёт это предложение, "
                     "если вы что-то поправили.")
        self._refine_set("\n".join(lines), fg=T.GOOD if same else T.ACCENT)
        pill = T.Pill(self.refine_box, "↺ Вернуть этот вариант", command=self._apply_refined,
                      style="ghost", bg=T.CARD_SOFT)
        pill._apply_pill = True
        pill.pack(anchor="w", padx=10, pady=(2, 8))
        self.refine_box.configure(highlightbackground=T.ACCENT)
        self._panel.canvas.yview_moveto(1.0)

    def _apply_refined(self, quiet: bool = False) -> None:
        if not self._refined:
            return
        ans = self._refined
        self.name_var.set(ans.get("new_name", ""))
        self.target_var.set(ans.get("target_path", ""))
        overall = ans.get("confidence", "")
        self.row["Уверенность имени"] = round(float(ans.get("name_confidence", overall) or 0), 2)
        self.row["Уверенность переноса"] = round(float(ans.get("move_confidence", overall) or 0), 2)
        self.row["Причина"] = ("перепроверка: " + str(ans.get("reason", "")))[:400]
        if not quiet:
            self._refine_set("✔ Улучшенный вариант подставлен в поля выше — проверьте и утвердите",
                             fg=T.GOOD)

    def show_prompt(self) -> None:
        from core.pipeline import build_prompt_preview

        text = build_prompt_preview(self.config, self.row.get("Текущий путь", ""))
        win = tk.Toplevel(self)
        win.title("Что уходит в модель")
        win.geometry("1000x700")
        win.transient(self)
        frame = tk.Frame(win, bg=T.CARD)
        frame.pack(fill="both", expand=True)
        box = tk.Text(frame, wrap="word", font=("Cascadia Mono", 10), padx=14, pady=12,
                      bg=T.CARD_SOFT, relief="flat", fg=T.INK)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        T.selectable_copy(box)
        bar = tk.Frame(win, bg=T.BG_TOP)
        bar.pack(fill="x")
        T.Pill(bar, "Копировать всё", command=lambda: (box.tag_add("sel", "1.0", "end-1c"),
                                                       box.event_generate("<Control-c>")),
               style="ghost", bg=T.BG_TOP).pack(side="left", padx=10, pady=6)
        T.Pill(bar, "Закрыть", command=win.destroy, style="quiet", bg=T.BG_TOP).pack(side="left", pady=6)

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        decision = self._decision.get()
        if decision == "ready" and not (self.target_var.get().strip().strip("/")
                                        and self.name_var.get().strip()):
            messagebox.showwarning(
                "Нельзя утвердить",
                "Для утверждения переноса нужна ЦЕЛЕВАЯ ПАПКА (и имя).\n\n"
                "Заполните «Целевая папка» справа или нажмите «🔬 Перепроверить глубже», чтобы "
                "модель подобрала место. Пока папки нет — строка не уйдёт на перенос.",
                parent=self)
            return
        self.row["Новое имя"] = self.name_var.get().strip()
        self.row["Целевой путь"] = self.target_var.get().strip().strip("/")
        self.row["Путь ярлыка"] = self.link_var.get().strip().strip("/")
        self.row["Комментарий"] = self.comment.get("1.0", "end").strip()
        if decision == "ready":
            self.row["Статус"] = "готово"
            self.row["Применить"] = "TRUE" if self.row["Целевой путь"] and self.row["Новое имя"] else "FALSE"
        elif decision == "keep":
            self.row["Статус"] = "без изменений"
            self.row["Применить"] = "FALSE"
            self.row["Тип операции"] = "без изменений"
        elif decision == "reject":
            self.row["Статус"] = "отказ"
            self.row["Применить"] = "FALSE"
        else:
            self.row["Статус"] = "проверка"
            self.row["Применить"] = "FALSE"
        if self.on_save:
            self.on_save(self.row)
        self.destroy()

    def apply_now(self) -> None:
        """Перенести этот файл сразу (если есть имя и папка), затем закрыть."""
        if not (self.target_var.get().strip().strip("/") and self.name_var.get().strip()):
            messagebox.showwarning(
                "Нечего применять",
                "Для переноса нужны «Новое имя» и «Целевая папка».\n\nЗаполните их справа или "
                "нажмите «🔬 Перепроверить глубже». Если файл и так в порядке — выберите «Не трогать».",
                parent=self)
            return
        self.row["Новое имя"] = self.name_var.get().strip()
        self.row["Целевой путь"] = self.target_var.get().strip().strip("/")
        self.row["Путь ярлыка"] = self.link_var.get().strip().strip("/")
        self.row["Комментарий"] = self.comment.get("1.0", "end").strip()
        self.row["Статус"] = "готово"
        self.row["Применить"] = "TRUE"
        if self.on_apply:
            self.on_apply(self.row)
        elif self.on_save:
            self.on_save(self.row)
        self.destroy()


def open_viewer(master, config: dict, row: dict, on_save=None, on_apply=None) -> FileViewer:
    return FileViewer(master, config, row, on_save=on_save, on_apply=on_apply)

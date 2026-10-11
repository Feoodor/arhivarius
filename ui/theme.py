"""Тема оформления окна: мягкий градиент, «стеклянные» карточки, анимации.

Чистый tkinter не умеет размытие и сглаживание: многоугольники на Canvas рисуются
«лесенкой». Поэтому все скруглённые формы готовятся в Pillow в 3 раза больше
нужного размера, а затем уменьшаются с LANCZOS — края получаются настоящими
гладкими, как в Telegram и Notion. Тени — то же скругление с гауссовым размытием.

Движение — короткие твины на 80–160 мс (наведение, нажатие, смена вкладки,
переключатель). Текст на кнопке всегда читается: цвет подписи подбирается по
яркости подложки, а не берётся «как в стиле».

Всё, что нужно наружу: Gradient, Card, Pill, Segmented, Switch, Scrollable, TabBar.
"""
from __future__ import annotations

from ui import i18n as _I

import tkinter as tk
from tkinter import ttk

# Палитра: контрастнее — фон чуть глубже, текст темнее, рамки заметнее.
BG_TOP = "#eef0f4"
BG_BOTTOM = "#dfe3ea"
CARD = "#ffffff"
CARD_SOFT = "#f4f6f9"
BORDER = "#d3d9e2"
BORDER_STRONG = "#b6bfcc"
INK = "#0e1319"
MUTED = "#5c6773"
ACCENT = "#0a63c9"
ACCENT_DARK = "#084f9f"
ACCENT_SOFT = "#dbeafc"
GOOD = "#1e7e3b"
WARN = "#a34a08"
BAD = "#c1272d"

RADIUS = 18
PILL_H = 34                 # высота кнопок-пилюль: компактно, но попасть мышью легко
SHADOW = "#c9d1dd"
SS = 4                      # во сколько раз рисуем форму с запасом перед сжатием
_CACHE: dict[tuple, object] = {}
_CACHE_MAX = 900

_STYLES = {
    "ghost": (CARD_SOFT, INK, BORDER),
    "accent": (ACCENT, "#ffffff", ACCENT_DARK),
    "danger": ("#fdecea", BAD, "#f4c7c3"),
    "good": ("#e8f5ee", GOOD, "#bfe3cf"),
    "quiet": ("#eef2f7", MUTED, BG_TOP),
}


_FONTS: dict = {}


def font(spec: tuple):
    """tkfont.Font по описанию — для замера реальной ширины текста (а не «8 px на букву»)."""
    cached = _FONTS.get(spec)
    if cached is None:
        import tkinter.font as tkfont
        cached = tkfont.Font(font=spec)
        _FONTS[spec] = cached
    return cached


def text_width(text: str, spec: tuple = ("Segoe UI Semibold", 11)) -> int:
    try:
        return int(font(spec).measure(text))
    except tk.TclError:
        return int(len(text) * 8.2)


def reset_caches() -> None:
    """Забыть картинки и шрифты: они привязаны к конкретному окну Tk и не переживают его закрытия."""
    _FONTS.clear()
    _CACHE.clear()
    _CORNERS.clear()


def mix(color_a: str, color_b: str, part: float) -> str:
    """Смешать два цвета: part=0 → a, part=1 → b."""
    part = max(0.0, min(1.0, part))
    out = []
    for channel in (1, 3, 5):
        first = int(color_a[channel:channel + 2], 16)
        second = int(color_b[channel:channel + 2], 16)
        out.append(round(first + (second - first) * part))
    return "#%02x%02x%02x" % tuple(out)


def luminance(color: str) -> float:
    """0..1 — насколько светлый цвет, чтобы подобрать читаемый текст."""
    try:
        red = int(color[1:3], 16) / 255
        green = int(color[3:5], 16) / 255
        blue = int(color[5:7], 16) / 255
    except (ValueError, IndexError):
        return 1.0
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def readable(background: str, dark: str = INK, light: str = "#ffffff") -> str:
    return light if luminance(background) < 0.55 else dark


def quantize(color: str, step: int = 10) -> str:
    """Округлить цвет: анимация из 7 шагов не плодит десятки версий одной картинки."""
    try:
        parts = [min(255, (int(color[i:i + 2], 16) // step) * step) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return color
    return "#%02x%02x%02x" % tuple(parts)


def animate(widget, apply, steps: int = 8, done=None, delay: int = 16) -> None:
    """Короткий ease-out твин: apply(t) для t от 0 до 1, шаг — after виджета."""
    def tick(index: int) -> None:
        if index > steps:
            apply(1.0)
            if done:
                done()
            return
        progress = index / steps
        apply(1 - (1 - progress) ** 3)
        try:
            widget.after(delay, lambda: tick(index + 1))
        except tk.TclError:
            pass

    tick(0)


def _photo(width: int, height: int, radius: int, fill: str, outline: str, line: int, blur: int = 0):
    """Сглаженная картинка скруглённой формы: (PhotoImage, отступ) или (None, 0).

    Pillow рисует обводку и размытие за границами прямоугольника, поэтому вокруг
    фигуры оставляем поле — элемент ставится со смещением на величину отступа.
    """
    key = (width, height, radius, quantize(fill), quantize(outline), line, blur)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageTk
    except Exception:
        _CACHE[key] = (None, 0)
        return None, 0
    wide, high = max(1, width * SS), max(1, height * SS)
    pad = (blur or 0) * SS + (line * SS if outline else 0)
    canvas_w, canvas_h = wide + pad * 2, high + pad * 2
    image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = [pad, pad, pad + wide - 1, pad + high - 1]
    corner = max(0, min(radius * SS, (wide - 1) // 2, (high - 1) // 2))
    if blur:
        red = int(fill[1:3], 16)
        green = int(fill[3:5], 16)
        blue = int(fill[5:7], 16)
        draw.rounded_rectangle(box, radius=corner, fill=(red, green, blue, 70))
        image = image.filter(ImageFilter.GaussianBlur(blur * SS / 2.2))
    else:
        draw.rounded_rectangle(box, radius=corner,
                               fill=None if fill in ("", "none") else fill,
                               outline=outline or None,
                               width=max(1, line * SS) if outline else 0)
    image = image.resize((canvas_w // SS, canvas_h // SS), Image.LANCZOS)
    photo = ImageTk.PhotoImage(image)
    if len(_CACHE) > _CACHE_MAX:          # простая защита от разрастания кэша
        _CACHE.clear()
    _CACHE[key] = (photo, pad // SS)
    return photo, pad // SS


def _keep(canvas: tk.Canvas, photo) -> None:
    """Tk освобождает PhotoImage без ссылки на неё — держим её на холсте."""
    canvas._keep = getattr(canvas, "_keep", [])
    canvas._keep.append(photo)


def _prune_items(canvas: tk.Canvas) -> None:
    """Вызывать сразу после delete(): старые ссылки на картинки больше не нужны."""
    canvas._keep = []


class Gradient(tk.Canvas):
    """Мягкий вертикальный градиент фона; перерисовывается при изменении размера."""

    def __init__(self, master, top: str = BG_TOP, bottom: str = BG_BOTTOM, bands: int = 90, **kw):
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self.top, self.bottom, self.bands = top, bottom, bands
        self.bind("<Configure>", lambda _e: self.draw())

    def draw(self) -> None:
        self.delete("grad")
        height = self.winfo_height() or 1
        width = self.winfo_width() or 1
        step = height / self.bands
        for index in range(self.bands):
            color = mix(self.top, self.bottom, index / max(1, self.bands - 1))
            y = index * step
            self.create_rectangle(-2, y, width + 2, y + step + 1, fill=color, outline=color, tags="grad")


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: int, **kw):
    """Скруглённый прямоугольник. Гладкий, если доступен Pillow; иначе многоугольник."""
    fill = kw.get("fill", "")
    outline = kw.get("outline", "") or ""
    line = int(kw.get("width", 1) or 1)
    tags = kw.get("tags", ())
    width, height = int(x2 - x1), int(y2 - y1)
    if fill or outline:
        photo, off = _photo(width, height, int(radius), fill or "none", outline, line)
        if photo is not None:
            _keep(canvas, photo)
            return canvas.create_image(int(x1) - off, int(y1) - off, anchor="nw", image=photo, tags=tags)
    radius = min(radius, width / 2, height / 2)
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


def soft_shadow(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int,
                color: str = "#94a3b8", blur: int = 6, tags=()):
    """Мягкая тень: та же форма, но размытая и полупрозрачная."""
    photo, off = _photo(int(x2 - x1), int(y2 - y1), radius, color, "", 0, blur=blur)
    if photo is None:
        return None
    _keep(canvas, photo)
    return canvas.create_image(int(x1) - off, int(y1) - off, anchor="nw", image=photo, tags=tags)


def smooth_circle(canvas: tk.Canvas, cx: float, cy: float, r: float, fill: str,
                  outline: str = "", tags=()):
    """Идеально круглый векторный кружок: суперсэмплинг + LANCZOS, без «лесенки»."""
    d = max(2, int(round(r * 2)))
    photo, off = _photo(d, d, d // 2, fill, outline or fill, 1 if outline else 0)
    if photo is not None:
        _keep(canvas, photo)
        return canvas.create_image(int(cx), int(cy), image=photo, tags=tags)
    return canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=outline or "", tags=tags)


_CORNERS: dict[tuple, list] = {}


def _corner_photos(radius: int, fill: str, outline: str):
    """Четыре сглаженные четверти скруглённой рамки. Дёшево: размер зависит только от радиуса,
    а не от размера карточки (раньше на каждое изменение окна рисовалась огромная картинка)."""
    key = (radius, fill, outline)
    if key in _CORNERS:
        return _CORNERS[key]
    try:
        from PIL import Image, ImageDraw, ImageTk
    except Exception:
        _CORNERS[key] = None
        return None
    side = radius * 2 * SS
    big = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse((0, 0, side - 1, side - 1), fill=fill, outline=outline, width=SS)
    small = big.resize((radius * 2, radius * 2), Image.LANCZOS)
    r = radius
    quarters = [small.crop(box) for box in ((0, 0, r, r), (r, 0, 2 * r, r), (r, r, 2 * r, 2 * r), (0, r, r, 2 * r))]
    photos = [ImageTk.PhotoImage(q) for q in quarters]
    _CORNERS[key] = photos
    return photos


class Card(tk.Frame):
    """Карточка: белая плашка с тонкой рамкой и скруглёнными углами поверх градиента.

    Размер карточки задаёт содержимое (``inner``) — обычное pack-распространение,
    без ручной подгонки высоты. Подложку рисует холст позади; ``inner`` упакован
    сам, поэтому снаружи достаточно ``card.pack(...)`` и класть виджеты в ``card.inner``.
    """

    def __init__(self, master, padding: int = 16, tone: str = CARD, radius: int = RADIUS,
                 shadow: bool = True, fill: bool = False, **kw):
        super().__init__(master, bg=kw.pop("container", BG_TOP), **kw)
        self.tone, self.radius, self.padding = tone, min(radius, 14), padding
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=self["bg"])
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.inner = tk.Frame(self, bg=tone)
        self.inner.pack(fill="both", expand=True, padx=padding, pady=padding)
        self.canvas.bind("<Configure>", self._redraw)

    def _redraw(self, event=None) -> None:
        canvas = self.canvas
        width = event.width if event else canvas.winfo_width()
        height = event.height if event else canvas.winfo_height()
        canvas.delete("shape")
        _prune_items(canvas)
        if width <= 4 or height <= 4:
            return
        r = max(2, min(self.radius, width // 2, height // 2))
        fill, line = self.tone, BORDER
        photos = _corner_photos(r, fill, line)
        if photos is None:
            canvas.create_rectangle(0, 0, width - 1, height - 1, fill=fill, outline=line, tags="shape")
            return
        canvas.create_rectangle(r, 0, width - r, height, fill=fill, outline=fill, tags="shape")
        canvas.create_rectangle(0, r, width, height - r, fill=fill, outline=fill, tags="shape")
        for photo, (x, y) in zip(photos, ((0, 0), (width - r, 0), (width - r, height - r), (0, height - r))):
            _keep(canvas, photo)
            canvas.create_image(x, y, anchor="nw", image=photo, tags="shape")
        for x1, y1, x2, y2 in ((r, 0, width - r, 0), (r, height - 1, width - r, height - 1),
                               (0, r, 0, height - r), (width - 1, r, width - 1, height - r)):
            canvas.create_line(x1, y1, x2, y2, fill=line, tags="shape")
        canvas.tag_lower("shape")


class Flow(tk.Frame):
    """Панель кнопок с переносом на следующую строку — ничего не уезжает за правый край.

    Создавайте виджеты с ``master=flow`` и добавляйте ``flow.add(widget)``.
    ``newline=True`` — начать с новой строки, ``stretch=True`` — растянуть на всю ширину
    (подпись-пояснение)."""

    def __init__(self, master, bg: str = BG_TOP, gap: int = 6, row_gap: int = 6, **kw):
        super().__init__(master, bg=bg, **kw)
        self.gap, self.row_gap = gap, row_gap
        self._items: list[tuple[tk.Widget, bool, bool]] = []
        self._width = 0
        self.configure(height=40)
        self.bind("<Configure>", self._on_configure)
        self._sizes: tuple = ()
        self._poll_id = self.after(350, self._poll)
        self.bind("<Destroy>", self._stop_poll, add="+")

    def _stop_poll(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            self.after_cancel(self._poll_id)
        except (tk.TclError, ValueError):
            pass

    def _poll(self) -> None:
        """Подпись внутри панели сменила текст (статус, прогресс) → её нужная ширина другая: переложить."""
        try:
            if self.winfo_ismapped():
                sizes = tuple((w.winfo_reqwidth(), w.winfo_reqheight()) for w, _n, _s in self._items)
                if sizes != self._sizes:
                    self._sizes = sizes
                    self.relayout()
            self._poll_id = self.after(350, self._poll)
        except tk.TclError:
            pass

    def add(self, widget: tk.Widget, newline: bool = False, stretch: bool = False) -> tk.Widget:
        self._items.append((widget, newline, stretch))
        widget.place(x=0, y=0)
        self.after_idle(self.relayout)
        return widget

    def _on_configure(self, event) -> None:
        if event.width != self._width:
            self._width = event.width
            self.relayout()

    def relayout(self) -> None:
        width = self._width or self.winfo_width()
        if width <= 1:
            return
        x = y = row_h = 0
        for widget, newline, stretch in self._items:
            try:
                if stretch:
                    widget.configure(wraplength=max(200, width - 4))
                    widget.update_idletasks()
                w, h = widget.winfo_reqwidth(), widget.winfo_reqheight()
            except tk.TclError:
                continue
            if stretch:
                w = width
            w = min(w, width)
            if newline or stretch or (x and x + w > width):
                if x:
                    y += row_h + self.row_gap
                x, row_h = 0, 0
            widget.place(x=x, y=y, width=w, height=h)
            x += w + self.gap
            row_h = max(row_h, h)
            if stretch:
                y += row_h + self.row_gap
                x, row_h = 0, 0
        total = y + row_h
        if abs(int(self.cget("height")) - total) > 1:
            self.configure(height=max(1, total))


class Pill(tk.Canvas):
    """Кнопка-пилюля с анимацией наведения и нажатия; подпись читаема всегда."""

    def __init__(self, master, text: str, command=None, style: str = "ghost", width: int = 0,
                 icon: str = "", enabled: bool = True, **kw):
        text = _I.tr(text)
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self.command, self.text, self.icon = command, text, icon
        self.enabled = enabled
        self.set_style(style, draw=False)
        natural = 30 + text_width((f"{icon}  " if icon else "") + text)
        self.configure(width=width or natural, height=PILL_H, bg=self._bg_of_master())
        self.bind("<Enter>", lambda _e: self._animate(self.hover_face, -2))
        self.bind("<Leave>", lambda _e: self._animate(self.face, 0))
        self.bind("<ButtonPress-1>", lambda _e: self._animate(self.press_face, 1))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda _e: self.draw())
        self.draw()

    def set_style(self, style: str, draw: bool = True) -> None:
        """Сменить стиль и пересчитать все производные цвета — иначе hover «съедает» подпись."""
        self.style = style
        self.face, self.ink, self.edge = _STYLES.get(style, _STYLES["ghost"])
        self.hover_face = mix(self.face, "#ffffff" if style != "accent" else ACCENT_DARK, 0.45)
        self.press_face = mix(self.face, "#000000", 0.08)
        self._anim = getattr(self, "_anim", 0) + 1     # обрываем старый твин
        self._face, self._lift = self.face, 0
        if draw:
            self.draw()

    def _bg_of_master(self) -> str:
        try:
            return self.master.cget("bg") if isinstance(self.master.cget("bg"), str) else BG_TOP
        except tk.TclError:
            return BG_TOP

    def _release(self, event) -> None:
        # отпустили кнопку мыши за пределами пилюли — это отмена, а не нажатие
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._animate(self.hover_face if inside else self.face, -2 if inside else 0)
        if inside and self.enabled and self.command:
            self.command()

    def _animate(self, face: str, lift: int) -> None:
        start, start_lift = self._face, self._lift
        self._anim = getattr(self, "_anim", 0) + 1
        token = self._anim

        def apply(t: float) -> None:
            if self._anim != token:          # более свежее нажатие/наведение уже всё перекрасило
                return
            self._face = mix(start, face, t)
            self._lift = round(start_lift + (lift - start_lift) * t)
            self.draw()

        animate(self, apply, steps=7)

    def set_enabled(self, value: bool) -> None:
        self.enabled = value
        self.draw()

    def configure_text(self, text: str) -> None:
        self.text = _I.tr(text)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        _prune_items(self)
        width, height = self.winfo_width() or int(self["width"]), (self.winfo_height() or PILL_H) - self._lift
        radius = min(PILL_H // 2, height // 2)
        face = self._face if self.enabled else mix(self._face, "#ffffff", 0.55)
        rounded_rect(self, 1, 1 + self._lift, width - 1, height - 1, radius,
                     fill=face, outline=self.edge if self.enabled else BORDER, width=1)
        self.create_text(width // 2, (height + 1 + self._lift) // 2,
                         text=(f"{self.icon}  " if self.icon else "") + self.text, anchor="center",
                         fill=readable(face) if self.enabled else MUTED,
                         font=("Segoe UI Semibold", 11))


class Segmented(tk.Canvas):
    """Сегментированный переключатель с плавно скользящим индикатором."""

    def __init__(self, master, options: tuple[str, ...], command=None, **kw):
        options = tuple(_I.tr(x) for x in options)
        super().__init__(master, highlightthickness=0, bd=0, height=40, **kw)
        self.options, self.command = list(options), command
        self.selected = 0
        self._offset = 0.0
        self.bind("<Configure>", lambda _e: (self._layout(), self.draw()))
        self.bind("<ButtonRelease-1>", self._click)

    def _layout(self) -> None:
        width = self.winfo_width() or 300
        self.cell = width / max(1, len(self.options))

    def select(self, index: int, slide: bool = True) -> None:
        index = max(0, min(len(self.options) - 1, index))
        start = self._offset
        self.selected = index

        def apply(t: float) -> None:
            self._offset = start + (index - start) * t
            self.draw()

        if slide:
            animate(self, apply, steps=9)
        else:
            self._offset = index
            self.draw()

    def _click(self, event) -> None:
        self._layout()
        index = int(event.x // max(1, self.cell))
        if 0 <= index < len(self.options) and index != self.selected:
            self.select(index)
            if self.command:
                self.command(index, self.options[index])

    def draw(self) -> None:
        self.delete("all")
        _prune_items(self)
        width, height = self.winfo_width() or 300, 38
        rounded_rect(self, 1, 1, width - 1, height, 19, fill=CARD_SOFT, outline=BORDER)
        self._layout()
        x = 3 + self._offset * self.cell
        rounded_rect(self, x, 3, x + self.cell - 3, height - 3, 16, fill=CARD, outline=BORDER_STRONG)
        for index, option in enumerate(self.options):
            center = self.cell * (index + 0.5)
            self.create_text(center, height / 2, text=option,
                             fill=INK if index == self.selected else MUTED,
                             font=("Segoe UI Semibold" if index == self.selected else "Segoe UI", 11))


class Switch(tk.Canvas):
    """Мини-переключатель с анимацией ползунка."""

    def __init__(self, master, variable=None, command=None, **kw):
        super().__init__(master, width=46, height=26, highlightthickness=0, bd=0, **kw)
        self.variable, self.command = variable, command
        self._pos = 1.0 if (variable and variable.get()) else 0.0
        self.bind("<ButtonRelease-1>", self._toggle)
        self.draw()

    def _toggle(self, _e) -> None:
        value = not bool(self.variable.get()) if self.variable else True
        if self.variable:
            self.variable.set(value)
        start = self._pos
        target = 1.0 if value else 0.0

        def apply(t: float) -> None:
            self._pos = start + (target - start) * t
            self.draw()

        animate(self, apply, steps=8)
        if self.command:
            self.command(value)

    def draw(self) -> None:
        self.delete("all")
        _prune_items(self)
        value = bool(self.variable.get()) if self.variable else self._pos > 0.5
        track = mix(BORDER_STRONG, ACCENT, 1.0 if value else self._pos)
        rounded_rect(self, 1, 3, 45, 23, 10, fill=track, outline=track)
        smooth_circle(self, 8 + self._pos * 24 + 8, 13, 8, "#ffffff", outline=BORDER_STRONG)


class Scrollable(ttk.Frame):
    """Страница с вертикальной прокруткой.

    Если содержимое ниже окна — прокручивается колесом/полосой; если короче — растягивается
    на всю высоту, и виджеты с ``expand=True`` (таблицы, тексты) занимают весь экран.
    Колесо мыши обрабатывает ``ui.input`` (прокручивает вложенное поле, потом страницу)."""

    def __init__(self, master, bg: str = BG_TOP, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=bg,
                                highlightbackground=bg, yscrollincrement=40)
        self.canvas._page_scroll = True
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self._set_bar)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._bar_shown = False
        self._cw = self._ch = 1
        self._last = (0, 0, 0)
        self.body.bind("<Configure>", lambda _e: self._fit())
        self.canvas.bind("<Configure>", self._on_canvas)
        self._poll_id = self.after(300, self._poll)
        self.bind("<Destroy>", self._stop_poll, add="+")

    def _set_bar(self, first, last) -> None:
        self.bar.set(first, last)
        need = float(first) > 0.0 or float(last) < 1.0
        if need != self._bar_shown:
            self._bar_shown = need
            if need:
                self.bar.pack(side="right", fill="y", before=self.canvas)
            else:
                self.bar.pack_forget()

    def _on_canvas(self, event) -> None:
        self._cw, self._ch = event.width, event.height
        self._fit()

    def _fit(self) -> None:
        need = self.body.winfo_reqheight()
        height = max(self._ch, need)
        state = (self._cw, height, need)
        if state == self._last:
            return
        self._last = state
        self.canvas.itemconfigure(self._window, width=self._cw, height=height)
        self.canvas.configure(scrollregion=(0, 0, self._cw, height))

    def _stop_poll(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            self.after_cancel(self._poll_id)
        except (tk.TclError, ValueError):
            pass
        self._poll_id = ""

    def _poll(self) -> None:
        """Содержимое могло вырасти без изменения размера окна (перенос панели кнопок, новый текст)."""
        try:
            if self.winfo_ismapped():
                self._fit()
            self._poll_id = self.after(300, self._poll)
        except tk.TclError:
            pass

    def scroll_to_top(self, slide: bool = True) -> None:
        start = self.canvas.yview()[0]

        def apply(t: float) -> None:
            self.canvas.yview_moveto(start * (1 - t))

        animate(self.canvas, apply, steps=8) if slide else self.canvas.yview_moveto(0)

    # прежний API: обработкой колеса теперь занимается ui.input
    def grab_wheel(self, active: bool) -> None:
        return None

    def bind_wheel_to(self, *widgets) -> None:
        return None


class TabBar(Flow):
    """Верхняя навигация: пилюли с эмодзи; на узком окне переносятся на вторую строку."""

    def __init__(self, master, pages: list[tuple[str, str]], command, **kw):
        pages = [(k, _I.tr(title)) for k, title in pages]
        super().__init__(master, bg=kw.pop("bg", BG_TOP), **kw)
        self.pages, self.command = pages, command
        self.pills: dict[str, Pill] = {}
        self.selected = pages[0][0]
        for key, title in pages:
            pill = Pill(self, title, command=lambda k=key: self.select(k),
                        style="accent" if key == self.selected else "ghost", bg=self["bg"])
            self.add(pill)
            self.pills[key] = pill

    def select(self, key: str) -> None:
        if key == self.selected:
            return
        for name, pill in self.pills.items():
            pill.set_style("accent" if name == key else "ghost")
        self.selected = key
        self.command(key)


def fade_in(widget: tk.Widget, color_from: str = ACCENT_SOFT, color_to: str = BG_TOP) -> None:
    """Мягкое появление страницы: едва заметный переход фона, без молочной вспышки."""
    color_from = mix(color_from, color_to, 0.62)
    try:
        current = widget.cget("bg")
    except tk.TclError:
        return
    target = color_to if not isinstance(current, str) or current in ("", BG_TOP) else current

    def apply(t: float) -> None:
        try:
            widget.configure(bg=mix(color_from, target, t))
            for child in widget.winfo_children():
                if isinstance(child, (tk.Frame, ttk.Frame)):
                    try:
                        child.configure(bg=mix(color_from, target, t))
                    except tk.TclError:
                        pass
        except tk.TclError:
            pass

    animate(widget, apply, steps=6)


def selectable_copy(text: tk.Text, copy_all_hint: str = "") -> None:
    """Текст, который можно выделять и копировать, но нельзя случайно испортить.

    disabled-виджет в tkinter не выделяется мышью, поэтому поле остаётся «редактируемым»,
    а любые попытки ввода перехватываются. Ctrl+C / Ctrl+A / правый клик обслуживает
    ``ui.input`` (на любой раскладке); поле помечено ``_ro_guard`` — вставка и вырезание запрещены."""
    def block(event):
        if event.state & 0x0004:          # Ctrl+… разрешаем: копирование и выделение
            return None
        if event.keysym in ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next"):
            return None                  # навигация курсором не портит текст
        return "break"

    for key in (
        "Key", "space", "Return", "BackSpace", "Delete", "Tab", "Insert", "KP_Enter", "KP_0", "KP_Add",
        "Alt-Key", "Control-BackSpace",
    ):
        text.bind(f"<{key}>", block)
    text._ro_guard = True
    text.configure(cursor="xterm")

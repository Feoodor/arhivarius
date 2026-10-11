"""Ввод: колесо мыши, буфер обмена и контекстные меню — одинаково на любой раскладке.

Что здесь исправлено относительно «голого» tkinter:

* Ctrl+C / Ctrl+V / Ctrl+X / Ctrl+A не работали на русской раскладке: Tk привязывает
  их к ЛАТИНСКИМ keysym (``c``), а при русской раскладке приходит ``Cyrillic_es``.
  Комбинации распознаются по keysym и по коду клавиши, поэтому работают везде.
* Колесо мыши: прокручивается тот виджет, над которым стоит указатель (таблица,
  текст, список), а когда он дошёл до края — страница целиком. Выпадающие списки
  и счётчики не меняют значение от случайной прокрутки страницы настроек.
  Shift+колесо — горизонтальная прокрутка таблиц.
* Таблицы копируются: Ctrl+C — выделенные строки (столбцы через табуляцию, вставляется
  в Excel), правый клик — меню «копировать строку / ячейку / путь».
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk
from typing import Callable

_CYRILLIC = {"cyrillic_ef": "a", "cyrillic_es": "c", "cyrillic_em": "v", "cyrillic_che": "x"}
_WIN_VK = {65: "a", 67: "c", 86: "v", 88: "x"}          # виртуальные коды Windows: от раскладки не зависят
_X11_KEYCODE = {38: "a", 54: "c", 55: "v", 53: "x"}     # evdev: запасной вариант для Linux
_CTRL_BIT = 0x0004


def _alt_bit() -> int:
    """Бит «Alt» в event.state. На Windows это 0x20000 (а 0x8 там — NumLock, он обычно включён!),
    на X11 Alt = Mod1 = 0x8. AltGr (= Ctrl+Alt) вводит символ — копированием его считать нельзя."""
    return 0x20000 if sys.platform == "win32" else 0x0008

ENTRY_CLASSES = ("Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox")
TEXT_CLASSES = ("Text",)


def shortcut_of(event) -> str:
    """'a' | 'c' | 'v' | 'x' | '' — какую из комбинаций Ctrl+? нажали, на любой раскладке."""
    state = int(getattr(event, "state", 0) or 0)
    if not state & _CTRL_BIT or state & _alt_bit():
        return ""
    sym = str(getattr(event, "keysym", "")).lower()
    if sym in ("a", "c", "v", "x"):
        return sym
    if sym in _CYRILLIC:
        return _CYRILLIC[sym]
    code = int(getattr(event, "keycode", 0) or 0)
    if sys.platform == "win32":
        return _WIN_VK.get(code, "")
    if sys.platform.startswith("linux"):
        return _X11_KEYCODE.get(code, "")
    return ""


# ------------------------------------------------------------------ буфер обмена
def _cls(widget) -> str:
    try:
        return widget.winfo_class()
    except (tk.TclError, AttributeError):
        return ""


def is_readonly(widget) -> bool:
    """Текст только для чтения: выделять и копировать можно, менять — нет."""
    if getattr(widget, "_ro_guard", False):
        return True
    try:
        state = str(widget.cget("state"))
    except (tk.TclError, AttributeError):
        return False
    return state in ("disabled", "readonly")


def selected_text(widget) -> str:
    """Выделенный текст виджета; '' — выделения нет."""
    cls = _cls(widget)
    try:
        if cls in TEXT_CLASSES:
            return widget.get("sel.first", "sel.last")
        if cls in ENTRY_CLASSES:
            if widget.selection_present():
                return widget.selection_get()
    except tk.TclError:
        pass
    return ""


def whole_text(widget) -> str:
    cls = _cls(widget)
    try:
        if cls in TEXT_CLASSES:
            return widget.get("1.0", "end-1c")
        if cls in ENTRY_CLASSES:
            return str(widget.get())
    except tk.TclError:
        pass
    return ""


def put_on_clipboard(widget, text: str) -> None:
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
        widget.update_idletasks()     # на Windows без этого содержимое иногда пропадает при закрытии окна
    except tk.TclError:
        pass


def do_copy(widget, cut: bool = False) -> str:
    """Копировать (или вырезать) выделенное. Нет выделения у read-only поля — копируем всё."""
    text = selected_text(widget)
    had_selection = bool(text)
    if not text and is_readonly(widget):
        text = whole_text(widget)
    if text:
        put_on_clipboard(widget, text)
    if cut and had_selection and not is_readonly(widget):
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
    return "break"


def do_paste(widget) -> str:
    if is_readonly(widget):
        return "break"
    try:
        data = widget.clipboard_get()
    except tk.TclError:
        return "break"
    try:
        if _cls(widget) in TEXT_CLASSES:
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            widget.insert("insert", data)
            widget.see("insert")
        elif _cls(widget) in ENTRY_CLASSES:
            line = (data.splitlines() or [""])[0]          # в однострочное поле — только первая строка
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            widget.insert("insert", line)
    except tk.TclError:
        pass
    return "break"


def do_select_all(widget) -> str:
    try:
        cls = _cls(widget)
        if cls in TEXT_CLASSES:
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "end-1c")
        elif cls in ENTRY_CLASSES:
            widget.selection_range(0, "end")
            widget.icursor("end")
        elif cls == "Treeview":
            hook = getattr(widget, "_on_select_all", None)
            if hook:
                hook()
            else:
                widget.selection_set(widget.get_children(""))
    except tk.TclError:
        pass
    return "break"


# ------------------------------------------------------------------ таблицы
def tree_columns(tree: ttk.Treeview) -> list[str]:
    """Столбцы в порядке показа (включая «дерево» #0, если оно видно)."""
    cols = list(tree["displaycolumns"]) if tree["displaycolumns"] not in ((), ("#all",)) else list(tree["columns"])
    if cols == ["#all"]:
        cols = list(tree["columns"])
    if "tree" in str(tree["show"]):
        cols = ["#0"] + cols
    return cols


def row_values(tree: ttk.Treeview, iid: str, columns: list[str] | None = None) -> list[str]:
    columns = columns or tree_columns(tree)
    out = []
    for col in columns:
        if col == "#0":
            out.append(str(tree.item(iid, "text")))
        else:
            out.append(str(tree.set(iid, col)))
    return out


def tree_text(tree: ttk.Treeview, with_header: bool = False, iids: list[str] | None = None) -> str:
    """Выделенные строки таблицы: столбцы через табуляцию, строки через перевод строки."""
    ids = list(iids if iids is not None else tree.selection())
    if not ids:
        return ""
    columns = tree_columns(tree)
    lines = []
    if with_header:
        lines.append("\t".join(str(tree.heading(c, "text")).strip(" ▲▼") for c in columns))
    for iid in ids:
        lines.append("\t".join(v.replace("\t", " ").replace("\n", " ") for v in row_values(tree, iid, columns)))
    return "\n".join(lines)


def cell_under(tree: ttk.Treeview, event) -> tuple[str, str]:
    """(iid, столбец) под указателем; пустые строки, если там пусто."""
    iid = tree.identify_row(event.y)
    col = tree.identify_column(event.x)       # '#3' — номер в порядке показа
    columns = tree_columns(tree)
    try:
        number = int(col.lstrip("#")) - (0 if "tree" in str(tree["show"]) else 1)
        name = columns[number] if 0 <= number < len(columns) else ""
    except ValueError:
        name = ""
    return iid, name


def attach_tree(tree: ttk.Treeview, extra: list[tuple[str, Callable[[], None]]] | None = None,
                on_select_all: Callable[[], None] | None = None) -> tk.Menu:
    """Меню правой кнопки и Ctrl+C для таблицы. extra — свои пункты (название, действие)."""
    if on_select_all:
        tree._on_select_all = on_select_all
    menu = tk.Menu(tree, tearoff=0)
    state = {"cell": ""}

    def copy_rows(header: bool = False) -> None:
        text = tree_text(tree, with_header=header)
        if text:
            put_on_clipboard(tree, text)

    def copy_cell() -> None:
        if state["cell"]:
            put_on_clipboard(tree, state["cell"])

    def show(event) -> str:
        iid, col = cell_under(tree, event)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        state["cell"] = ""
        if iid and col:
            vals = row_values(tree, iid, [col])
            state["cell"] = vals[0] if vals else ""
        menu.delete(0, "end")
        menu.add_command(label="Копировать строки", command=copy_rows,
                         state="normal" if tree.selection() else "disabled")
        menu.add_command(label="Копировать с заголовками", command=lambda: copy_rows(True),
                         state="normal" if tree.selection() else "disabled")
        menu.add_command(label="Копировать ячейку", command=copy_cell,
                         state="normal" if state["cell"] else "disabled")
        if extra:
            menu.add_separator()
            for label, action in extra:
                menu.add_command(label=label, command=action,
                                 state="normal" if tree.selection() else "disabled")
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: do_select_all(tree))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    tree.bind("<Button-3>", show)
    tree.bind("<Button-2>", show)      # macOS
    tree._copy_menu = menu
    return menu


# ------------------------------------------------------------------ колесо мыши
def _can_scroll(widget, delta: int, horizontal: bool) -> bool:
    try:
        first, last = (widget.xview() if horizontal else widget.yview())
    except (tk.TclError, AttributeError):
        return False
    if first <= 0.0 and last >= 1.0:
        return False
    return (delta > 0 and first > 0.0005) or (delta < 0 and last < 0.9995)


def route_wheel(widget, delta: int, horizontal: bool = False) -> bool:
    """Прокрутить ближайший (от виджета вверх по родителям) прокручиваемый предок. True — сработало."""
    count = max(1, round(abs(delta) / 120)) if abs(delta) >= 60 else 1
    node = widget
    while node is not None:
        cls = _cls(node)
        scrollable = cls in ("Text", "Treeview", "Listbox") or getattr(node, "_page_scroll", False)
        if scrollable and _can_scroll(node, delta, horizontal):
            step = (-count if delta > 0 else count)
            if getattr(node, "_page_scroll", False):
                node.yview_scroll(step * 2, "units")          # холст страницы: 2 × 40 px
            elif horizontal:
                node.xview_scroll(step * 3, "units")
            else:
                node.yview_scroll(step * 3, "units")
            return True
        node = getattr(node, "master", None)
    return False


def _pointer_widget(event):
    try:
        found = event.widget.winfo_containing(event.x_root, event.y_root)
        if found is not None:
            return found
    except (tk.TclError, KeyError):
        pass
    return event.widget


def _on_wheel(event) -> str | None:
    delta = int(getattr(event, "delta", 0) or 0)
    num = getattr(event, "num", 0)
    if num == 4:
        delta = 120
    elif num == 5:
        delta = -120
    if not delta:
        return None
    horizontal = bool(int(getattr(event, "state", 0) or 0) & 0x0001)      # Shift
    route_wheel(_pointer_widget(event), delta, horizontal)
    return "break"          # даже если прокручивать нечего: штатное поведение класса (смена значения) не нужно


def handle_key(event, kind: str | None = None) -> str | None:
    """Ctrl+C/V/X/A для виджета события. kind — готовая буква (латинская привязка) или None (определить)."""
    key = kind or shortcut_of(event)
    if not key:
        return None
    widget = event.widget
    if _cls(widget) == "Treeview":
        if key == "c":
            text = tree_text(widget)
            if text:
                put_on_clipboard(widget, text)
            return "break"
        if key == "a":
            return do_select_all(widget)
        return None
    if key == "c":
        return do_copy(widget)
    if key == "x":
        return do_copy(widget, cut=True)
    if key == "v":
        return do_paste(widget)
    if key == "a":
        return do_select_all(widget)
    return None


# ------------------------------------------------------------------ установка
def install(root: tk.Misc) -> None:
    """Включить колесо, горячие клавиши и меню для всего приложения (один раз)."""
    if getattr(root, "_input_installed", False):
        return
    root._input_installed = True

    for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(seq, _on_wheel)
        for cls in ("Text", "Treeview", "Listbox", "TCombobox", "TSpinbox", "Spinbox", "TScale",
                    "TNotebook", "Canvas"):
            try:
                root.bind_class(cls, seq, _on_wheel)         # заменяет штатное поведение класса
            except tk.TclError:
                continue

    def make_key_handler(kind: str | None = None):
        return lambda event: handle_key(event, kind)

    generic = make_key_handler()
    for cls in (*ENTRY_CLASSES, *TEXT_CLASSES, "Treeview"):
        try:
            root.bind_class(cls, "<Control-KeyPress>", generic, add="+")
            for letter in "acvx":                              # латиница: единое поведение вместо штатного
                for variant in (letter, letter.upper()):
                    root.bind_class(cls, f"<Control-Key-{variant}>", make_key_handler(letter))
            if cls != "Treeview":
                root.bind_class(cls, "<<Copy>>", lambda e: do_copy(e.widget))
                root.bind_class(cls, "<<Cut>>", lambda e: do_copy(e.widget, cut=True))
                root.bind_class(cls, "<<Paste>>", lambda e: do_paste(e.widget))
        except tk.TclError:
            continue

    # правый клик в полях ввода и текстах
    menu = tk.Menu(root, tearoff=0)
    target = {"w": None}

    def act(fn):
        return lambda: fn(target["w"]) if target["w"] is not None else None

    menu.add_command(label="Копировать", command=act(do_copy))
    menu.add_command(label="Вставить", command=act(do_paste))
    menu.add_command(label="Вырезать", command=act(lambda w: do_copy(w, cut=True)))
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=act(do_select_all))

    def show(event):
        widget = event.widget
        target["w"] = widget
        ro = is_readonly(widget)
        have = bool(selected_text(widget)) or (ro and bool(whole_text(widget)))
        menu.entryconfigure(0, state="normal" if have else "disabled")
        menu.entryconfigure(1, state="disabled" if ro else "normal")
        menu.entryconfigure(2, state="disabled" if ro or not selected_text(widget) else "normal")
        try:
            widget.focus_set()
        except tk.TclError:
            pass
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    for cls in (*ENTRY_CLASSES, *TEXT_CLASSES):
        try:
            root.bind_class(cls, "<Button-3>", show)
            root.bind_class(cls, "<Button-2>", show)
        except tk.TclError:
            continue
    root._input_menu = menu

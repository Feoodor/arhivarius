"""Общие окна и системные действия: открыть файл/папку, показать в проводнике, текст с копированием."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from ui import input as uinput
from ui import theme as T


def open_path(path: Path | str) -> None:
    """Открыть файл или папку программой по умолчанию (Windows, macOS, Linux).

    Нет такой папки (например, «_output» до первого запуска) — создаём её; нет файла — FileNotFoundError."""
    path = Path(path)
    if not path.exists():
        if path.suffix:
            raise FileNotFoundError(str(path))
        path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal_path(path: Path | str) -> None:
    """Показать файл в проводнике, выделив его (на Linux — открыть папку)."""
    path = Path(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def text_window(master, title: str, text: str, *, size: str = "980x640", wrap: str = "word",
                on_copy: Callable[[], None] | None = None) -> tk.Toplevel:
    """Окно с текстом: прокрутка, выделение мышью, «Копировать всё», закрытие по Esc."""
    win = tk.Toplevel(master)
    win.title(title)
    win.geometry(size)
    win.minsize(520, 320)
    win.configure(bg=T.CARD)
    bar = T.Flow(win, bg=T.CARD)
    bar.pack(side="bottom", fill="x", padx=12, pady=8)
    holder = tk.Frame(win, bg=T.CARD)
    holder.pack(fill="both", expand=True)
    holder.grid_rowconfigure(0, weight=1)
    holder.grid_columnconfigure(0, weight=1)
    box = tk.Text(holder, wrap=wrap, font=("Cascadia Mono", 10), padx=12, pady=10, bg=T.CARD, fg=T.INK,
                  relief="flat")
    ybar = ttk.Scrollbar(holder, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=ybar.set)
    box.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")
    if wrap == "none":
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=box.xview)
        box.configure(xscrollcommand=xbar.set)
        xbar.grid(row=1, column=0, sticky="ew")
    box.insert("1.0", text)
    T.selectable_copy(box)

    def copy_all() -> None:
        uinput.put_on_clipboard(box, box.get("1.0", "end-1c"))
        if on_copy:
            on_copy()

    bar.add(T.Pill(bar, "📋 Копировать всё", command=copy_all, style="accent", bg=T.CARD))
    bar.add(T.Pill(bar, "Закрыть", command=win.destroy, style="quiet", bg=T.CARD))
    win.bind("<Escape>", lambda _e: win.destroy())
    win.text = box
    box.focus_set()
    return win

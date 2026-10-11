"""Интерфейс: копирование на любой раскладке, прокрутка, перенос кнопок, окно целиком.

Нужен дисплей: на сервере запускайте через ``xvfb-run -a python -m pytest tests/test_ui.py``.
Без дисплея (или без tkinter) тесты помечаются пропущенными, а не упавшими."""
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

from ui import input as uinput          # сам модуль дисплея не требует: логика сочетаний клавиш тестируется и без него

try:
    _probe = tk.Tk()
    _probe.destroy()
    HAVE_DISPLAY = True
except Exception:          # нет DISPLAY
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from ui import theme as T


class Ev:
    """Событие клавиатуры, как его видит Tk: на русской раскладке keysym кириллический.

    Xvfb не умеет выдать кириллический keysym (Tk показывает «??»), поэтому русскую
    раскладку проверяем, вызывая обработчик напрямую с таким событием."""

    def __init__(self, widget, keysym: str, keycode: int = 0, state: int = 0x4):
        self.widget, self.keysym, self.keycode, self.state = widget, keysym, keycode, state


def destroy_clean(root) -> None:
    """Закрыть окно, отменив отложенные вызовы: иначе на Windows они срабатывают в следующем окне
    теста и печатают «invalid command name» (на работу программы это не влияет)."""
    try:
        for after_id in root.tk.splitlist(root.tk.call("after", "info")):
            try:
                root.tk.call("after", "cancel", after_id)       # напрямую: after_cancel() падает на служебных вызовах Tk
            except tk.TclError:
                pass
    except tk.TclError:
        pass
    root.destroy()


def pump(root, seconds: float = 0.05) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.005)


@unittest.skipUnless(HAVE_DISPLAY, "нет дисплея: запустите через xvfb-run")
class Base(unittest.TestCase):
    def setUp(self):
        T.reset_caches()
        self.root = tk.Tk()
        self.root.geometry("700x500+0+0")
        uinput.install(self.root)
        self.root.update()

    def tearDown(self):
        destroy_clean(self.root)

    def press(self, widget, keysym: str, ctrl: bool = True):
        widget.focus_force()
        self.root.update()
        widget.event_generate(f"<{'Control-' if ctrl else ''}Key-{keysym}>")
        self.root.update()

    def clip(self) -> str:
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            return ""


class Shortcuts(unittest.TestCase):
    """Какую комбинацию Ctrl+? распознаёт программа — на любой раскладке и ОС."""

    def of(self, platform: str, **kw) -> str:
        from unittest import mock
        with mock.patch.object(uinput.sys, "platform", platform):
            return uinput.shortcut_of(Ev(None, **kw))

    def test_latin_and_cyrillic_on_linux(self):
        self.assertEqual(self.of("linux", keysym="c"), "c")
        for sym, want in (("Cyrillic_es", "c"), ("Cyrillic_ES", "c"), ("Cyrillic_em", "v"),
                          ("Cyrillic_che", "x"), ("Cyrillic_ef", "a")):
            self.assertEqual(self.of("linux", keysym=sym), want, sym)
        self.assertEqual(self.of("linux", keysym="Cyrillic_es", state=0x4 | 0x2), "c")      # CapsLock
        self.assertEqual(self.of("linux", keysym="Cyrillic_es", state=0x4 | 0x10), "c")     # NumLock (Mod2)

    def test_windows_keycode_independent_of_layout(self):
        self.assertEqual(self.of("win32", keysym="Cyrillic_es", keycode=67), "c")
        self.assertEqual(self.of("win32", keysym="??", keycode=86), "v")
        self.assertEqual(self.of("win32", keysym="Greek_psi", keycode=88), "x")

    def test_windows_numlock_does_not_disable_shortcuts(self):
        """На Windows бит 0x8 — NumLock (обычно включён), а не Alt."""
        self.assertEqual(self.of("win32", keysym="c", state=0x4 | 0x8), "c")
        self.assertEqual(self.of("win32", keysym="Cyrillic_es", keycode=67, state=0x4 | 0x8), "c")

    def test_altgr_and_missing_ctrl_are_ignored(self):
        self.assertEqual(self.of("win32", keysym="Cyrillic_es", keycode=67, state=0x4 | 0x20000), "")
        self.assertEqual(self.of("linux", keysym="c", state=0x4 | 0x8), "")                 # Ctrl+Alt+C
        self.assertEqual(self.of("linux", keysym="c", state=0), "")
        self.assertEqual(self.of("linux", keysym="Cyrillic_es", state=0), "")

    def test_linux_keycode_fallback(self):
        self.assertEqual(self.of("linux", keysym="??", keycode=54), "c")
        self.assertEqual(self.of("linux", keysym="??", keycode=9), "")


class Clipboard(Base):
    def test_entry_copy_cut_paste_select_all_latin(self):
        var = tk.StringVar(value="Привет мир")
        entry = ttk.Entry(self.root, textvariable=var)
        entry.pack()
        self.root.clipboard_clear()
        self.press(entry, "a")
        self.assertTrue(entry.selection_present(), "Ctrl+A не выделил текст")
        self.press(entry, "c")
        self.assertEqual(self.clip(), "Привет мир")
        self.press(entry, "x")
        self.assertEqual(var.get(), "")
        self.press(entry, "v")
        self.assertEqual(var.get(), "Привет мир")           # вставилось РОВНО один раз

    def test_entry_all_shortcuts_on_russian_layout(self):
        for sym_set in (("Cyrillic_es", "Cyrillic_em", "Cyrillic_che", "Cyrillic_ef"),
                        ("Cyrillic_ES", "Cyrillic_EM", "Cyrillic_CHE", "Cyrillic_EF")):
            copy, paste, cut, all_ = sym_set
            var = tk.StringVar(value="Привет мир")
            entry = ttk.Entry(self.root, textvariable=var)
            entry.pack()
            self.root.clipboard_clear()
            uinput.handle_key(Ev(entry, all_))
            self.assertTrue(entry.selection_present(), "Ctrl+Ф не выделил текст")
            uinput.handle_key(Ev(entry, copy))
            self.assertEqual(self.clip(), "Привет мир")
            uinput.handle_key(Ev(entry, cut))
            self.assertEqual(var.get(), "")
            uinput.handle_key(Ev(entry, paste))
            self.assertEqual(var.get(), "Привет мир")
            entry.destroy()

    def test_latin_paste_not_doubled_in_text(self):
        box = tk.Text(self.root)
        box.pack()
        self.root.clipboard_clear()
        self.root.clipboard_append("абв")
        self.press(box, "v")
        self.assertEqual(box.get("1.0", "end-1c"), "абв")
        uinput.handle_key(Ev(box, "Cyrillic_em"))
        self.assertEqual(box.get("1.0", "end-1c"), "абвабв")

    def test_readonly_text_copies_but_cannot_be_edited(self):
        box = tk.Text(self.root)
        box.pack()
        box.insert("1.0", "строка журнала\nвторая")
        T.selectable_copy(box)
        self.root.clipboard_clear()
        box.tag_add("sel", "1.0", "1.14")
        uinput.handle_key(Ev(box, "Cyrillic_es"))
        self.assertEqual(self.clip(), "строка журнала")
        self.root.clipboard_clear()
        self.root.clipboard_append("ЧУЖОЕ")
        uinput.handle_key(Ev(box, "Cyrillic_em"))                   # вставка запрещена
        self.press(box, "v")
        uinput.handle_key(Ev(box, "Cyrillic_che"))                  # вырезание запрещено
        self.assertEqual(box.get("1.0", "end-1c"), "строка журнала\nвторая")
        # ввод с клавиатуры блокируется, навигация нет
        box.focus_force()
        box.event_generate("<Key-x>")
        box.event_generate("<BackSpace>")
        self.assertEqual(box.get("1.0", "end-1c"), "строка журнала\nвторая")
        # без выделения у read-only поля копируется всё
        box.tag_remove("sel", "1.0", "end")
        self.root.clipboard_clear()
        self.press(box, "c")
        self.assertEqual(self.clip(), "строка журнала\nвторая")

    def test_tree_copy_gives_tab_separated_rows(self):
        tree = ttk.Treeview(self.root, columns=("a", "b"), show="headings")
        tree.heading("a", text="Было")
        tree.heading("b", text="Станет")
        tree.pack()
        tree.insert("", "end", iid="1", values=("x/y.txt", "2026_Письмо.txt"))
        tree.insert("", "end", iid="2", values=("p/q.txt", "2026_Рапорт.txt"))
        tree.selection_set(("1", "2"))
        want = "x/y.txt\t2026_Письмо.txt\np/q.txt\t2026_Рапорт.txt"
        self.root.clipboard_clear()
        self.press(tree, "c")
        self.assertEqual(self.clip(), want)
        self.root.clipboard_clear()
        uinput.handle_key(Ev(tree, "Cyrillic_es"))
        self.assertEqual(self.clip(), want)
        self.assertEqual(uinput.tree_text(tree, with_header=True).splitlines()[0], "Было\tСтанет")

    def test_tree_select_all_uses_hook_in_any_layout(self):
        tree = ttk.Treeview(self.root, columns=("a",), show="headings")
        tree.pack()
        for i in range(3):
            tree.insert("", "end", iid=str(i), values=(i,))
        calls = []
        uinput.attach_tree(tree, on_select_all=lambda: (calls.append(1), tree.selection_set(tree.get_children())))
        uinput.handle_key(Ev(tree, "Cyrillic_ef"))
        self.assertEqual(len(tree.selection()), 3)
        self.assertEqual(calls, [1])


class Wheel(Base):
    def make_page(self, rows: int = 60):
        page = T.Scrollable(self.root)
        page.pack(fill="both", expand=True)
        for i in range(rows):
            tk.Label(page.body, text=f"строка {i}", bg=T.BG_TOP).pack(anchor="w", pady=4)
        pump(self.root, 0.3)
        return page

    def wheel(self, widget, delta: int):
        widget.event_generate("<MouseWheel>", delta=delta, rootx=widget.winfo_rootx() + 4,
                              rooty=widget.winfo_rooty() + 4)
        self.root.update()

    def test_page_scrolls_down_and_up(self):
        page = self.make_page()
        label = page.body.winfo_children()[3]
        self.assertGreater(page.canvas.yview()[1] - 0.0, 0)
        self.assertLess(page.canvas.yview()[1], 1.0)
        for _ in range(5):
            self.wheel(label, -120)
        moved = page.canvas.yview()[0]
        self.assertGreater(moved, 0.0, "колесо не прокрутило страницу")
        for _ in range(5):
            self.wheel(label, 120)
        self.assertLess(page.canvas.yview()[0], moved)

    def test_page_scrolls_on_linux_buttons(self):
        page = self.make_page()
        label = page.body.winfo_children()[3]
        label.event_generate("<Button-5>", rootx=label.winfo_rootx() + 4, rooty=label.winfo_rooty() + 4)
        self.root.update()
        self.assertGreater(page.canvas.yview()[0], 0.0)

    def test_inner_text_scrolls_first_then_page(self):
        page = T.Scrollable(self.root)
        page.pack(fill="both", expand=True)
        box = tk.Text(page.body, height=4)                    # поле вверху страницы — оно под указателем
        box.pack(fill="x")
        box.insert("1.0", "\n".join(f"line {i}" for i in range(80)))
        for i in range(40):
            tk.Label(page.body, text=f"ниже {i}", bg=T.BG_TOP).pack(pady=10)
        pump(self.root, 0.3)
        self.wheel(box, -120)
        self.assertGreater(box.yview()[0], 0.0, "текст не прокрутился")
        self.assertEqual(page.canvas.yview()[0], 0.0, "страница поехала, хотя текст ещё не дошёл до конца")
        box.yview_moveto(1.0)
        self.root.update()
        self.wheel(box, -120)                                  # текст на краю — едет страница
        self.assertGreater(page.canvas.yview()[0], 0.0)

    def test_combobox_and_spinbox_do_not_change_on_wheel(self):
        page = self.make_page(rows=30)
        var = tk.StringVar(value="b")
        combo = ttk.Combobox(page.body, textvariable=var, values=("a", "b", "c"), state="readonly")
        combo.pack()
        spin_var = tk.IntVar(value=8192)
        spin = ttk.Spinbox(page.body, from_=4096, to=65536, increment=4096, textvariable=spin_var)
        spin.pack()
        pump(self.root, 0.2)
        self.wheel(combo, -120)
        self.wheel(combo, 120)
        self.wheel(spin, -120)
        self.wheel(spin, 120)
        self.assertEqual(var.get(), "b")
        self.assertEqual(spin_var.get(), 8192)

    def test_short_page_fills_height_so_tables_expand(self):
        page = T.Scrollable(self.root)
        page.pack(fill="both", expand=True)
        tree = ttk.Treeview(page.body, height=3)
        tree.pack(fill="both", expand=True)
        pump(self.root, 0.3)
        self.assertGreater(tree.winfo_height(), page.winfo_height() * 0.8)
        self.assertFalse(page._bar_shown, "полоса прокрутки не нужна, когда всё помещается")


class Widgets(Base):
    def test_pill_release_outside_does_not_fire(self):
        hits = []
        pill = T.Pill(self.root, "Тест", command=lambda: hits.append(1))
        pill.pack(padx=40, pady=40)
        pump(self.root, 0.1)
        pill.event_generate("<ButtonPress-1>", x=5, y=5)
        pill.event_generate("<ButtonRelease-1>", x=pill.winfo_width() + 50, y=5)
        self.root.update()
        self.assertEqual(hits, [])
        pill.event_generate("<ButtonPress-1>", x=5, y=5)
        pill.event_generate("<ButtonRelease-1>", x=5, y=5)
        self.root.update()
        self.assertEqual(hits, [1])

    def test_pill_width_follows_real_text(self):
        short = T.Pill(self.root, "ОК")
        long = T.Pill(self.root, "Переразобрать выбранное")
        self.assertGreater(int(long["width"]), int(short["width"]) + 100)
        self.assertGreaterEqual(int(long["width"]), T.text_width("Переразобрать выбранное"))

    def test_flow_wraps_and_stays_inside(self):
        flow = T.Flow(self.root, bg=T.BG_TOP)
        flow.pack(fill="x")
        pills = [flow.add(T.Pill(flow, f"Кнопка номер {i}", bg=T.BG_TOP)) for i in range(8)]
        self.root.geometry("420x500+0+0")
        pump(self.root, 0.4)
        width = flow.winfo_width()
        rows = {p.winfo_y() for p in pills}
        self.assertGreaterEqual(len(rows), 2, "кнопки не перенеслись на новую строку")
        for pill in pills:
            self.assertGreaterEqual(pill.winfo_x(), 0)
            self.assertLessEqual(pill.winfo_x() + pill.winfo_width(), width + 1,
                                 "кнопка выехала за правый край")
        self.assertGreaterEqual(flow.winfo_height(), max(p.winfo_y() + p.winfo_height() for p in pills) - 1)

    def test_flow_note_takes_full_row(self):
        flow = T.Flow(self.root)
        flow.pack(fill="x")
        flow.add(T.Pill(flow, "A"))
        note = tk.Label(flow, text="длинное пояснение " * 12)
        flow.add(note, stretch=True)
        pump(self.root, 0.3)
        self.assertGreater(note.winfo_y(), 0)
        self.assertGreaterEqual(note.winfo_width(), flow.winfo_width() - 2)

    def test_card_sizes_to_content_and_draws_border(self):
        card = T.Card(self.root, padding=12)
        card.pack(fill="x")
        tk.Label(card.inner, text="внутри").pack()
        pump(self.root, 0.3)
        self.assertGreater(card.winfo_height(), 30)
        self.assertGreater(len(card.canvas.find_withtag("shape")), 4)


@unittest.skipUnless(HAVE_DISPLAY, "нет дисплея: запустите через xvfb-run")
class MainWindow(unittest.TestCase):
    """Окно целиком на поддельной Ollama: все вкладки открываются, ничего не выезжает за край."""

    def test_every_page_opens_and_nothing_overflows(self):
        from tests.fake_ollama import FakeOllama
        from tests.helpers import department_responder, make_archive
        from ui.main_window import PAGES, OrganizerApp

        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            config, _ = make_archive(Path(tmp), None, {"base_url": server.url})
            app = OrganizerApp(config)
            try:
                app.geometry("1100x700+0+0")
                pump(app, 0.8)
                app.update_idletasks()
                for key, _title in PAGES:
                    app.tabbar.select(key)
                    pump(app, 0.3)
                    page = app.pages[key]
                    self.assertTrue(page.winfo_ismapped(), key)
                    limit = app.winfo_width() + 2
                    for widget in _walk(page.body):
                        if isinstance(widget, T.Pill) and widget.winfo_ismapped():
                            right = widget.winfo_rootx() + widget.winfo_width() - app.winfo_rootx()
                            self.assertLessEqual(right, limit, f"{key}: кнопка «{widget.text}» за краем окна")
                # шапка: «Открыть результаты» не перекрывает статус Ollama
                self.assertLess(app.ollama_label.winfo_rooty() + app.ollama_label.winfo_height(),
                                app.ollama_label.master.master.winfo_rooty() + 200)
            finally:
                destroy_clean(app)

    def test_shortcuts_work_in_real_window(self):
        from tests.fake_ollama import FakeOllama
        from tests.helpers import department_responder, make_archive
        from ui.main_window import OrganizerApp

        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            config, _ = make_archive(Path(tmp), None, {"base_url": server.url})
            app = OrganizerApp(config)
            try:
                app.geometry("1100x700+0+0")
                pump(app, 0.5)
                app.append_log("строка журнала для копирования", "ok")
                app.tabbar.select("log")
                pump(app, 0.2)
                app.log_text.focus_force()
                app.update()
                app.clipboard_clear()
                uinput.handle_key(Ev(app.log_text, "Cyrillic_ef"))             # Ctrl+Ф = выделить всё
                uinput.handle_key(Ev(app.log_text, "Cyrillic_es"))             # Ctrl+С = копировать
                self.assertIn("строка журнала для копирования", app.clipboard_get())
            finally:
                destroy_clean(app)


@unittest.skipUnless(HAVE_DISPLAY, "нет дисплея: запустите через xvfb-run")
class Viewer(unittest.TestCase):
    def test_text_file_content_is_visible_and_copyable(self):
        """Регресс: превью текстового файла было скрыто (pack_forget без повторного pack)."""
        from ui.viewer import FileViewer

        T.reset_caches()
        root = tk.Tk()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "a.txt").write_text("Содержимое файла для просмотра", encoding="utf-8")
                cfg = {"scan_root": Path(tmp), "app_dir": Path(tmp), "llm": {}, "thresholds": {}}
                viewer = FileViewer(root, cfg, {"Текущий путь": "a.txt", "Статус": "проверка"})
                pump(root, 0.6)
                self.assertTrue(viewer.preview.winfo_ismapped(), "превью текста не показано")
                self.assertIn("Содержимое файла", viewer.preview.get("1.0", "end"))
                self.assertGreater(viewer.preview.winfo_height(), 100)
                viewer.preview.focus_force()
                uinput.install(root)
                root.clipboard_clear()
                uinput.handle_key(Ev(viewer.preview, "Cyrillic_ef"))      # Ctrl+Ф
                uinput.handle_key(Ev(viewer.preview, "Cyrillic_es"))      # Ctrl+С
                self.assertIn("Содержимое файла для просмотра", root.clipboard_get())
                # нижняя панель кнопок не выходит за окно
                viewer.geometry("940x600+0+0")
                pump(root, 0.4)
                for widget in _walk(viewer):
                    if isinstance(widget, T.Pill) and widget.winfo_ismapped():
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width() - viewer.winfo_rootx(),
                                             viewer.winfo_width() + 1, widget.text)
                viewer.destroy()
        finally:
            destroy_clean(root)


def _walk(widget):
    stack = [widget]
    while stack:
        node = stack.pop()
        yield node
        try:
            stack.extend(node.winfo_children())
        except tk.TclError:
            continue


if __name__ == "__main__":
    unittest.main()

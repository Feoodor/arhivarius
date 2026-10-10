"""Окно «Папки» — операции над структурой, а не над отдельными файлами.

Три раздела: пустые папки (с меткой «не трогать»), слияние одноимённых двойников
в канон и навигатор «папки из Разобрать → постоянное место» (отдельный проход
модели, план в folder_plan.json). Всё — с журналом folder_ops.csv и откатом
пачки; удаления файлов нет: пустые папки уходят в корзину Windows.
"""
from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import messagebox

from core import folders_ops, folder_router
from ui import i18n as I
from ui import theme as T


class FoldersWindow(tk.Toplevel):
    def __init__(self, master, config: dict, log=None):
        super().__init__(master)
        self.config = config
        self.log = log or (lambda m: None)
        self.title(I.tr("Папки · пустые, двойники, навигатор зоны разбора"))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1180, sw - 80)}x{min(760, sh - 100)}")
        self.minsize(860, 540)
        self.busy = False
        self._empty: list[tuple[tk.BooleanVar, str]] = []
        self._merges: list[tuple[tk.BooleanVar, dict]] = []
        self._nav: list[tuple[tk.BooleanVar, dict]] = []
        self._build()
        self.refresh_all()

    # ------------------------------------------------------------- каркас
    def _build(self) -> None:
        bar = tk.Frame(self, bg=T.BG_TOP)
        bar.pack(fill="x", padx=14, pady=(10, 4))
        T.Pill(bar, I.tr("↻ обновить всё"), command=self.refresh_all, style="ghost").pack(side="left")
        T.Pill(bar, I.tr("Откатить последнюю пачку"), command=self.undo_last, style="ghost").pack(side="left", padx=6)
        T.Pill(bar, "Журнал folder_ops.csv",
               command=lambda: self._open_file(self.config["output_path"] / folders_ops.JOURNAL_NAME),
               style="ghost").pack(side="left")
        self.status = tk.Label(bar, text="", bg=T.BG_TOP, fg=T.MUTED, font=("Segoe UI", 9))
        self.status.pack(side="right")
        self.scroller = T.Scrollable(self, bg=T.BG_TOP)
        self.scroller.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    def _card(self, title: str) -> tk.Frame:
        card = T.Card(self.scroller.body, padding=14, tone=T.CARD)
        card.pack(fill="x", pady=(0, 12))
        card.inner.pack(fill="x")
        tk.Label(card.inner, text=title, bg=T.CARD, fg=T.INK,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
        return card.inner

    def _open_file(self, path: Path) -> None:
        try:
            import os
            if path.exists():
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                messagebox.showinfo("Журнал", "Файл журнала ещё не создан.")
        except OSError as exc:
            self.log(f"не открыл журнал: {exc}")

    def _run_bg(self, work, done) -> None:
        if self.busy:
            messagebox.showinfo("Занято", "Дождитесь окончания текущего действия.")
            return
        self.busy = True
        self.status.configure(text="работаем…")

        def wrap():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                result = exc
            self.after(0, lambda: self._finish(done, result))

        threading.Thread(target=wrap, daemon=True).start()

    def _finish(self, done, result) -> None:
        self.busy = False
        if isinstance(result, Exception):
            self.status.configure(text=f"сбой: {type(result).__name__}: {str(result)[:80]}")
            self.log(f"окно «Папки»: сбой {type(result).__name__}: {result}")
        else:
            done(result)

    # ------------------------------------------------------------- разделы
    def refresh_all(self) -> None:
        for child in list(self.scroller.body.winfo_children()):
            child.destroy()
        self._empty, self._merges, self._nav = [], [], []
        self._build_empties()
        self._build_merges()
        self._build_navigator()
        self._build_taxonomy()

    # пустые ───────────────────────────────────────────────────────────────
    def _build_empties(self) -> None:
        card = self._card(I.tr("🧹 Пустые папки"))
        try:
            candidates, kept = folders_ops.find_empty_folders(self.config)
        except OSError as exc:
            tk.Label(card, text=f"нет доступа к диску: {exc}", bg=T.CARD, fg=T.WARN).pack(anchor="w")
            return
        for rel in candidates:
            row = tk.Frame(card, bg=T.CARD_SOFT)
            row.pack(fill="x", pady=2, padx=4)
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(row, text=rel, variable=var, bg=T.CARD_SOFT, fg=T.INK,
                           activebackground=T.CARD_SOFT, selectcolor=T.CARD, anchor="w",
                           font=("Segoe UI", 10), wraplength=920, justify="left").pack(
                side="left", fill="x", expand=True)
            T.Pill(row, "не трогать", command=lambda r=rel: self._mark_keep(r, True),
                   style="ghost").pack(side="right")
            self._empty.append((var, rel))
        for rel in kept:
            row = tk.Frame(card, bg=T.CARD_SOFT)
            row.pack(fill="x", pady=2, padx=4)
            tk.Label(row, text=f"{rel}   ·  не трогать", bg=T.CARD_SOFT, fg=T.MUTED,
                     font=("Segoe UI", 10), anchor="w").pack(side="left", fill="x", expand=True)
            T.Pill(row, "вернуть в список", command=lambda r=rel: self._mark_keep(r, False),
                   style="ghost").pack(side="right")
        foot = tk.Frame(card, bg=T.CARD)
        foot.pack(fill="x", pady=(6, 2))
        if self._empty:
            T.Pill(foot, I.tr("🗑 убрать помеченные в корзину Windows"), command=self.apply_empties,
                   style="accent").pack(side="left")
        tk.Label(foot, text=("из корзины восстанавливаются штатно; файлов это не касается"
                             if self._empty else "пустых папок нет 🎉"),
                 bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9)).pack(side="left", padx=8)

    def _mark_keep(self, rel: str, keep: bool) -> None:
        folders_ops.mark_keep(self.config, rel, keep)
        self.refresh_all()

    def apply_empties(self) -> None:
        paths = [rel for var, rel in self._empty if var.get()]
        if not paths:
            messagebox.showinfo("Пустые папки", "Нечего убирать — отметьте галочками.")
            return
        if not messagebox.askyesno(
                "Корзина", f"{len(paths)} пустых папок уйдут в корзину Windows (восстановимо).\n"
                           "Ни один файл не удаляется. Продолжить?"):
            return
        ok = fail = 0
        for rel in paths:
            good, msg = folders_ops.recycle_folder(self.config, rel)
            ok += good
            fail += (not good)
            if not good:
                self.log(f"  не убрал «{rel}»: {msg}")
        self.status.configure(text=f"в корзину: {ok}, пропусков: {fail}")
        self.refresh_all()

    # слияния ──────────────────────────────────────────────────────────────
    def _build_merges(self) -> None:
        from core import pipeline
        card = self._card(I.tr("🧲 Одноимённые папки — слить в канон"))
        try:
            _snap, index = pipeline._snapshot_and_index(self.config)
            groups = folders_ops.find_merge_groups(self.config, index)
        except FileNotFoundError:
            tk.Label(card, text="нет снимка — сначала обновите его на странице «Обзор»",
                     bg=T.CARD, fg=T.WARN).pack(anchor="w")
            return
        shown = 0
        for g in groups:
            try:
                moves = folders_ops.plan_merge(self.config, g)
            except OSError:
                moves = []
            if not moves:
                continue
            shown += 1
            dups = g["duplicates"]
            text = (f"{g['canonical']}  ←  " + "; ".join(dups[:3])
                    + (f" (+{len(dups) - 3})" if len(dups) > 3 else "")
                    + f"   · перемещений: {len(moves)}")
            row = tk.Frame(card, bg=T.CARD_SOFT)
            row.pack(fill="x", pady=2, padx=4)
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(row, text=text, variable=var, bg=T.CARD_SOFT, fg=T.INK,
                           activebackground=T.CARD_SOFT, selectcolor=T.CARD, anchor="w",
                           font=("Segoe UI", 10), wraplength=920, justify="left").pack(
                side="left", fill="x", expand=True)
            self._merges.append((var, g))
        if not shown:
            tk.Label(card, text="двойников, годных к слиянию, не найдено", bg=T.CARD,
                     fg=T.MUTED).pack(anchor="w")
        else:
            foot = tk.Frame(card, bg=T.CARD)
            foot.pack(fill="x", pady=(6, 2))
            T.Pill(foot, I.tr("▶ слить помеченные"), command=self.apply_merges, style="accent").pack(side="left")
            tk.Label(foot, text="содержимое дублей переезжает в канон; файлы не удаляются, "
                                "осиротевшие оболочки попадут в список пустых выше",
                     bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9)).pack(side="left", padx=8)

    def apply_merges(self) -> None:
        chosen = [g for var, g in self._merges if var.get()]
        if not chosen:
            messagebox.showinfo("Слияние", "Отметьте галочками группы для слияния.")
            return
        total = sum(len(folders_ops.plan_merge(self.config, g)) for g in chosen)
        if not messagebox.askyesno("Слияние", f"{len(chosen)} групп, {total} перемещений.\n"
                                             "Всё пишется в журнал — можно откатить пачкой. Идём?"):
            return
        batch = self._new_batch("слияние")
        done = fail = 0
        for g in chosen:
            for move in folders_ops.plan_merge(self.config, g):
                r = folders_ops.apply_move(self.config, move, batch)
                done += bool(r.get("ok"))
                fail += not r.get("ok")
        folders_ops.invalidate_index_cache()
        self.status.configure(text=f"перенесено {done}, ошибок {fail} (пачка {batch})")
        self.refresh_all()

    # навигатор ────────────────────────────────────────────────────────────
    def _build_navigator(self) -> None:
        card = self._card(I.tr("🧭 Папки из «Разобрать» → постоянное место"))
        head = tk.Frame(card, bg=T.CARD)
        head.pack(fill="x")
        T.Pill(head, I.tr("опросить модель по всем папкам зоны"), command=self.run_navigator,
               style="accent").pack(side="left")
        T.Pill(head, I.tr("⏱ замерить скорость (текст + фото)"), command=self.run_speed_probe,
               style="ghost").pack(side="left", padx=6)
        self.probe_label = tk.Label(head, text="", bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9))
        self.probe_label.pack(side="left")
        tk.Label(head, text="отдельный контекстный проход: имена папок и файлов; текст читается "
                            "только при коллизии одноимённых мест; план — folder_plan.json",
                 bg=T.CARD, fg=T.MUTED, font=("Segoe UI", 9), wraplength=780,
                 justify="left").pack(side="left", padx=8)
        plan = folder_router.load_plan_safe(self.config)
        for entry in plan:
            try:
                snap_for_stats = json.loads((Path(self.config["output_path"]) / "snapshot.json").read_text("utf-8"))
                st = folders_ops.folder_stats(snap_for_stats, entry["откуда"])
                est = folders_ops.estimate(st, folders_ops.load_probe(self.config))
                stat_line = (f"файлов: {st['всего']} (текст {st['текст']} · картинки {st['картинки']})"
                             + (f" · {est}" if est else ""))
            except Exception:
                stat_line = ""
            if entry.get("section"):
                text = f"{entry['откуда']}  →  {entry['section']}/"
            elif entry.get("split"):
                text = f"{entry['откуда']}  →  разнесётся по файлам (split)"
            else:
                text = f"{entry['откуда']}  →  места нет ({entry.get('note') or entry.get('reason') or '?'})"
            text += f"   · {entry.get('confidence', 0)} · {str(entry.get('reason', ''))[:90]}"
            if stat_line:
                text += "\n   " + stat_line
            if entry.get("решение"):
                text += f"   ✔ {entry['решение']}"
            row = tk.Frame(card, bg=T.CARD_SOFT)
            row.pack(fill="x", pady=2, padx=4)
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(row, text=text, variable=var, bg=T.CARD_SOFT,
                           fg=T.GOOD if not entry.get("note") else T.WARN,
                           activebackground=T.CARD_SOFT, selectcolor=T.CARD, anchor="w",
                           font=("Segoe UI", 10), wraplength=920, justify="left").pack(
                side="left", fill="x", expand=True)
            self._nav.append((var, entry))
        if any(e.get("section") for e in plan):
            foot = tk.Frame(card, bg=T.CARD)
            foot.pack(fill="x", pady=(6, 2))
            T.Pill(foot, I.tr("▶ перенести помеченные целиком"), command=self.apply_navigator,
                   style="accent").pack(side="left")

    def run_navigator(self) -> None:
        def work():
            return folder_router.propose_all(self.config, log=self.log, limit=40)

        def done(rows):
            self.status.configure(text=f"план построен: {len(rows)} папок")
            self.refresh_all()

        self._run_bg(work, done)

    def apply_navigator(self) -> None:
        chosen = [entry for var, entry in self._nav if var.get() and entry.get("section")]
        if not chosen:
            messagebox.showinfo("Навигатор", "Отметьте папки с предложенным разделом.")
            return
        if not messagebox.askyesno(
                "Навигатор", f"Перенести {len(chosen)} папок целиком в предложенные разделы?\n"
                             "Одинаково названные папки на месте сольются (содержимое не теряется, "
                             "журнал с откатом)."):
            return
        batch = self._new_batch("навигатор")
        plan = folder_router.load_plan(self.config)
        by_src = {row["откуда"]: row for row in plan}
        ok = fail = 0
        for entry in chosen:
            r = folders_ops.move_folder(self.config, entry["откуда"], entry["section"], batch)
            row = by_src.get(entry["откуда"], entry)
            if r.get("ok"):
                ok += 1
                row["решение"] = f"перенесено в {entry['section']} ({batch})"
            else:
                fail += 1
                row["решение"] = f"сбой: {r.get('msg')}"
                self.log(f"  навигатор: {entry['откуда']} — {r.get('msg')}")
        folder_router.save_plan(self.config, list(by_src.values()))
        folders_ops.invalidate_index_cache()
        self.status.configure(text=f"перенесено {ok}, ошибок {fail}")
        self.refresh_all()

    # подсказки структуры ────────────────────────────────────────────────
    def _build_taxonomy(self) -> None:
        hints = folders_ops.taxonomy_hints(self.config)
        if not hints:
            return
        card = self._card(I.tr("🌱 Недостающие папки (модель просит их снова и снова)"))
        for h in hints:
            row = tk.Frame(card, bg=T.CARD_SOFT)
            row.pack(fill="x", pady=2, padx=4)
            tk.Label(row, text=f"{h['родитель']}/{h['имя']}/   ·  {h['сколько']}×",
                     bg=T.CARD_SOFT, fg=T.INK, font=("Segoe UI", 10), anchor="w").pack(
                side="left", fill="x", expand=True)
        foot = tk.Frame(card, bg=T.CARD)
        foot.pack(fill="x", pady=(6, 2))
        T.Pill(foot, I.tr("▶ создать эти папки (только mkdir)"), command=self.apply_taxonomy,
               style="good").pack(side="left")

    def apply_taxonomy(self) -> None:
        hints = folders_ops.taxonomy_hints(self.config)
        if not hints:
            return
        if not messagebox.askyesno("Таксономия", f"Создать {len(hints)} новых папок? Только mkdir, "
                                                 "переносов не будет."):
            return
        root = Path(str(self.config["scan_root"]))
        made = 0
        for h in hints:
            target = root / PurePosixPath(f"{h['родитель']}/{h['имя']}")
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                folders_ops.log_op(self.config, "mkdir", "", f"{h['родитель']}/{h['имя']}",
                                   "таксономия", "ok")
                made += 1
        self.status.configure(text=f"создано папок: {made}")
        folders_ops.invalidate_index_cache()
        self.refresh_all()

    def run_speed_probe(self) -> None:
        def work():
            return folders_ops.measure_speed(self.config, log=self.log)
        def done(res):
            if res.get("t_текст") or res.get("t_картинка"):
                self.probe_label.configure(
                    text=f"замер: текст {res.get('t_текст', '—')}с · картинка {res.get('t_картинка', '—')}с")
                self.refresh_all()
            else:
                self.status.configure(text="не удалось замерить (нет читаемых файлов?)")
        self._run_bg(work, done)

    # служебное ────────────────────────────────────────────────────────────
    def _new_batch(self, kind: str) -> str:
        return f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"

    def undo_last(self) -> None:
        journal = folders_ops.read_journal(self.config)
        batches = [r["Пачка"] for r in journal if r["Действие"] == "перенести"]
        if not batches:
            messagebox.showinfo("Откат", "Журнал переносов пуст.")
            return
        batch = batches[-1]
        if not messagebox.askyesno("Откат", f"Вернуть пачку «{batch}» на прежние места?"):
            return
        result = folders_ops.undo_batch(self.config, batch)
        folders_ops.invalidate_index_cache()
        self.status.configure(text=f"откат: {result}")
        self.refresh_all()

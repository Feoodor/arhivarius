"""Иерархический указатель папок для промпта.

Проблема: на диске 2700+ папок. Полный список (~160 КБ текста) в промпте
перегружает Ollama -> HTTP 500. Решение: на каждый файл собирается
*локальный* иерархический срез структуры:

  1. скелет  — верхние уровни, чтобы модель знала разделы регламента;
  2. якорь   — ветка, где лежит файл, и её соседи (обычно место и здесь);
  3. поиск   — папки, похожие по названию на имя файла и тему из текста;
  4. бюджет  — жёсткий лимит путей/символов с плавной деградацией.

Отрисовка — древовидный текст; линейные цепочки склеиваются в одну строку, что
сильно экономит токены на глубоких разреженных ветках.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable

NOISE_DIRS = {
    "__pycache__", ".git", ".svn", "node_modules", ".cache", "$recycle.bin",
    "system volume information", ".thumbnails", ".trash", "temp", "tmp",
}
STOP_TOKENS = {
    "файл", "файлы", "документ", "документы", "новый", "новая", "новое", "финал",
    "черновик", "копия", "версия", "отдел", "молодежный", "прочее", "разное",
    "архив", "папка", "scan", "img", "photo", "безымянный", "лист", "таблица",
}
ENDINGS = (
    "иями", "иях", "ами", "ях", "ов", "ев", "ин", "ым", "ом", "ем", "ах", "ии",
    "ый", "ий", "ой", "ая", "яя", "ое", "ее", "ие", "ые",
    "а", "я", "ы", "и", "у", "ю", "е", "о",
)
# Русский текст в токенайзере Gemma ≈ 2.6 символа на токен.
CHARS_PER_TOKEN = 2.6
# Символы, возникающие при неверной перекодировке имён из архивов (CP866/CP437).
GARBLED_CHARS = set("α¿⌐öñΓâ∩ú₧ÆÉäèîàï¢æçêëìíïµ¼½¾·░▒│¹ºÃÑ¡δφτßÉùôçæìéêāšļ")


def is_garbled(name: str) -> bool:
    """Похоже на «битую» кодировку, а не на реальное название папки."""
    if not name:
        return False
    junk = sum(1 for ch in name if ch in GARBLED_CHARS)
    if junk < 2:
        return False
    letters = [ch for ch in name if ch.isalpha()]
    if not letters:
        return True
    return junk / max(1, len(name)) > 0.2 or not (set("aeiouyаеёиоуыэюя") & set(name.lower()))


def norm(value: str) -> str:
    """Нормализация: нижний регистр, ё→е, только буквы и цифры (кириллица тоже)."""
    return re.sub(r"[^a-z0-9\u0430-\u044f]+", " ", value.lower().replace("ё", "е")).strip()


def tokens(value: str) -> list[str]:
    return [t for t in norm(value).split() if len(t) >= 3]


def stem(word: str) -> str:
    for end in ENDINGS:
        if len(word) > len(end) + 3 and word.endswith(end):
            return word[: -len(end)]
    return word


def keywords_from_text(text: str, limit: int = 14) -> list[str]:
    """Значимые слова из имени файла и содержимого — для поиска нужной папки."""
    counts: "OrderedDict[str, int]" = OrderedDict()
    for word in tokens(text):
        if len(word) < 4 or word in STOP_TOKENS:
            continue
        key = stem(word)
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:limit]]


class Node:
    __slots__ = ("name", "path", "depth", "parent", "children", "files", "total")

    def __init__(self, name: str, path: str, depth: int, parent: "Node | None"):
        self.name = name
        self.path = path
        self.depth = depth
        self.parent = parent
        self.children: "OrderedDict[str, Node]" = OrderedDict()
        self.files = 0
        self.total = 0


@dataclass
class ContextPack:
    """Готовый блок «СУЩЕСТВУЮЩИЕ ПАПКИ» для одного запроса."""

    text: str
    paths: int
    chars: int
    est_tokens: int
    levels: int
    hidden: int
    notes: list[str] = field(default_factory=list)
    # двухпроходный разбор: влезла ли карта целиком и какие пути предлагались модели
    fits: bool = True
    paths_list: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        base = f"{self.paths} путей, ~{self.est_tokens} токенов, {self.levels} ур."
        if self.hidden:
            base += f", скрыто {self.hidden}"
        if self.notes:
            base += " | сжатие: " + "; ".join(self.notes)
        return base


class FolderIndex:
    """Индекс структуры папок + генератор ограниченных срезов для промпта."""

    def __init__(
        self,
        folders: Iterable[str] = (),
        file_counts: dict[str, int] | None = None,
        root_label: str = "",
        prune: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ):
        self.root = Node("", "", 0, None)
        self.by_path: dict[str, Node] = {"": self.root}
        self.prune = {p.casefold() for p in prune} | NOISE_DIRS
        self.exclude = [str(p).replace("\\", "/").strip("/") for p in exclude if str(p).strip("/")]
        self.root_label = root_label
        self._tokens: dict[str, tuple[str, ...]] = {}
        self._last: dict[str, list[str]] = {}
        self._insert_many(folders)
        self._apply_counts(file_counts or {})
        self.recompute_totals()

    def excluded(self, path: str) -> bool:
        """Ветка скрыта из промпта (например временная «Разобрать» с ошибочной структурой)."""
        clean = (path or "").strip("/")
        return any(clean == pref or clean.startswith(pref + "/") for pref in self.exclude)

    # ------------------------------------------------------------------ build
    @classmethod
    def from_snapshot(cls, snapshot: dict, prune: Iterable[str] = (), exclude: Iterable[str] = ()) -> "FolderIndex":
        counts: dict[str, int] = {}
        for item in snapshot.get("files", []):
            parent = str(item.get("path", "")).replace("\\", "/").rpartition("/")[0]
            counts[parent] = counts.get(parent, 0) + 1
        folders = set(snapshot.get("folders", []))
        folders.update(p for p in counts if p)
        return cls(sorted(folders), counts, str(snapshot.get("root", "")), prune, exclude)

    def _keep_dir(self, name: str) -> bool:
        low = name.casefold()
        return low not in self.prune and not low.startswith(("~$", "._"))

    def _insert(self, rel_path: str) -> None:
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
        if not parts or any(not self._keep_dir(p) for p in parts):
            return
        node = self.root
        for i, part in enumerate(parts):
            child = node.children.get(part)
            if child is None:
                child = Node(part, "/".join(parts[: i + 1]), i + 1, node)
                node.children[part] = child
                self.by_path[child.path] = child
            node = child

    def _insert_many(self, rel_paths: Iterable[str]) -> None:
        for rel in rel_paths:
            self._insert(str(rel))

    def _apply_counts(self, file_counts: dict[str, int]) -> None:
        for rel, count in file_counts.items():
            node = self.by_path.get(str(rel).replace("\\", "/").strip("/"))
            if node is not None:
                node.files = int(count)

    def recompute_totals(self) -> None:
        def walk(node: Node) -> int:
            total = node.files
            for child in node.children.values():
                total += walk(child)
            node.total = total
            return total

        walk(self.root)

    @property
    def folder_count(self) -> int:
        return len(self.by_path) - 1

    @property
    def top_levels(self) -> set[str]:
        """Имена разделов первого уровня реального дерева (не только дети mo_target_prefix)."""
        return {path.split("/", 1)[0] for path in self.by_path if path}

    # -------------------------------------------------------------- tokenizing
    def _prepare(self) -> None:
        if self._tokens:
            return
        for path, node in self.by_path.items():
            if not path:
                continue
            self._tokens[path] = tuple(
                stem(t) for part in path.split("/") for t in tokens(part)
            )
            self._last[path] = [stem(t) for t in tokens(node.name)]

    def _score(self, path: str, keys: list[str]) -> int:
        self._prepare()
        all_tokens = self._tokens.get(path, ())
        last = self._last.get(path, [])
        score = 0
        for key in keys:
            head = key[:4]
            if key in last:
                score += 6
            elif any(t == key or t.startswith(head) or key.startswith(t[:4]) for t in last):
                score += 4
            elif key in all_tokens:
                score += 3
            elif any(t.startswith(key[:5]) for t in all_tokens):
                score += 1
        return score

    # ------------------------------------------------------------------ render
    def _kept_children(self, node: Node, keep: set[str]) -> list[Node]:
        return [c for c in node.children.values() if c.path in keep]

    def _render(self, keep: set[str], show_files: bool) -> str:
        lines: list[str] = []

        def emit(node: Node, indent: int) -> None:
            chain = [node.name]
            cur = node
            while True:
                kids = self._kept_children(cur, keep)
                if len(kids) == 1 and kids[0].files == 0:
                    chain.append(kids[0].name)
                    cur = kids[0]
                else:
                    break
            label = "/".join(chain)
            suffix = f"  ({cur.total})" if show_files and cur.total else ""
            lines.append(f"{'  ' * indent}{label}/{suffix}")
            for child in self._kept_children(cur, keep):
                emit(child, indent + 1)

        for top in self._kept_children(self.root, keep):
            emit(top, 0)
        return "\n".join(lines)

    # ------------------------------------------------------------------- build
    def build(
        self,
        anchor: str = "",
        keywords: list[str] | None = None,
        skeleton_depth: int = 2,
        anchor_depth: int = 3,
        sibling_depth: int = 1,
        search_hits: int = 25,
        max_paths: int = 300,
        budget_chars: int = 7000,
        show_files: bool = True,
        hide_garbled: bool = True,
        respect_exclude: bool = True,
        boost: list[str] | None = None,
    ) -> ContextPack:
        """Собрать срез структуры для одного файла.

        anchor — относительный путь папки, где сейчас лежит файл.
        """
        keywords = [k for k in (keywords or []) if k]
        anchor = (anchor or "").replace("\\", "/").strip("/")
        while anchor and anchor not in self.by_path:
            anchor = anchor.rpartition("/")[0]
        notes: list[str] = []

        def keep_set(skel: int, anc: int, sib: int, use_search: bool) -> set[str]:
            keep: set[str] = {""}

            def add_chain(path: str) -> None:
                parts = path.split("/")
                for i in range(1, len(parts) + 1):
                    keep.add("/".join(parts[:i]))

            def add_subtree(path: str, depth: int) -> None:
                node = self.by_path.get(path)
                if node is None or depth < 0:
                    return
                keep.add(path)
                if depth == 0:
                    return
                for child in node.children.values():
                    if hide_garbled and is_garbled(child.name):
                        continue
                    if respect_exclude and self.excluded(child.path):
                        continue
                    add_subtree(child.path, depth - 1)

            add_subtree("", skel)
            anchor_hidden = respect_exclude and self.excluded(anchor)
            if anchor and not anchor_hidden:
                add_chain(anchor)
                add_subtree(anchor, anc)
                parts = anchor.split("/")
                for i in range(max(0, len(parts) - sib), len(parts)):
                    parent = self.by_path.get("/".join(parts[:i]))
                    if parent:
                        for child in parent.children.values():
                            add_subtree(child.path, sib)
            for bp in (boost or [])[:20]:
                node = self.by_path.get(bp)
                if node is not None and not (respect_exclude and self.excluded(bp)):
                    add_chain(bp)
                    add_subtree(bp, 2)
            if use_search and keywords:
                hits = 0
                for path in sorted(
                    (p for p in self.by_path if p and not (respect_exclude and self.excluded(p))),
                    key=lambda p: (-self._score(p, keywords), p),
                ):
                    if hits >= search_hits:
                        break
                    if self._score(path, keywords) >= 4:
                        add_chain(path)
                        add_subtree(path, 2)
                        hits += 1
            return keep

        def measure(keep: set[str], files: bool) -> tuple[str, int]:
            block = self._render(keep, files)
            return block, len([ln for ln in block.splitlines() if ln.strip()])

        # Кандидаты от самого подробного к самому скромному: держим карту системы
        # максимально широкой, жертвуем сначала глубиной ветки файла и подбором.
        candidates: list[tuple[int, int, bool, bool, str]] = []
        for skel in range(max(2, skeleton_depth), 1, -1):
            for anc in sorted({max(1, anchor_depth), 3, 2, 1}, reverse=True):
                candidates.append((skel, anc, True, show_files, f"карта до {skel} ярусов"))
                candidates.append((skel, anc, False, show_files, f"карта до {skel} ярусов без подбора по названию"))
        candidates.append((2, 1, False, False, "карта сжата до 2 ярусов"))
        candidates.append((1, 1, False, False, "карта сжата до 1 яруса"))

        text, paths, notes = "", 0, []
        for skel, anc, use_search, files, label in candidates:
            text, paths = measure(keep_set(skel, anc, sibling_depth, use_search), files)
            if paths <= max_paths and len(text) <= budget_chars:
                notes = [label] if label != f"карта до {skeleton_depth} ярусов" else []
                break
            notes = [label + " — не влезла в бюджет"]
        else:
            notes = ["карта урезана до минимума, чтобы не переполнить контекст"]

        levels = max((len(ln) - len(ln.lstrip(" ")) for ln in text.splitlines() if ln), default=0) // 2 + 1
        return ContextPack(
            text=text,
            paths=paths,
            chars=len(text),
            est_tokens=int(len(text) / CHARS_PER_TOKEN),
            levels=levels,
            hidden=max(0, self.folder_count - paths),
            notes=notes,
        )

    # ------------------------------------------------- двухпроходная карта
    def keep_up_to(self, depth: int, hide_garbled: bool = True,
                   respect_exclude: bool = True) -> set[str]:
        """Скелет: все папки не глубже N ярусов (выбор раздела в проходе 1)."""
        keep: set[str] = {""}

        def walk(node: Node, level: int) -> None:
            if level > depth:
                return
            for child in node.children.values():
                if respect_exclude and self.excluded(child.path):
                    continue
                if hide_garbled and is_garbled(child.name):
                    continue
                keep.add(child.path)
                walk(child, level + 1)

        walk(self.root, 1)
        keep.discard("")
        return keep

    def subtree_keep(self, sections: Iterable[str], depth: int = 9, *,
                     show_other_roots: bool = True, anchor: str = "",
                     anchor_depth: int = 1, hide_garbled: bool = True) -> set[str]:
        """Полное поддерево выбранных разделов + соседи-корни одной строкой."""
        keep: set[str] = {""}

        def add_chain(path: str) -> None:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):
                keep.add("/".join(parts[:i]))

        def add_below(node: Node, budget: int) -> None:
            if budget < 0:
                return
            for child in node.children.values():
                if hide_garbled and is_garbled(child.name):
                    continue
                if self.excluded(child.path):
                    continue
                keep.add(child.path)
                add_below(child, budget - 1)

        for sec in sections:
            node = self.by_path.get((sec or "").strip("/"))
            if node is None:
                continue
            add_chain(node.path)
            add_below(node, depth)
        if show_other_roots:
            for child in self.root.children.values():
                if hide_garbled and is_garbled(child.name):
                    continue
                if self.excluded(child.path):
                    continue
                keep.add(child.path)
        anchor = (anchor or "").strip("/")
        if anchor and not self.excluded(anchor):
            node = self.by_path.get(anchor)
            if node is not None:
                add_chain(anchor)
                add_below(node, anchor_depth)
        keep.discard("")
        return keep

    def pack_from_keep(self, keep: set[str], show_files: bool, budget_chars: int,
                       flat: bool = False) -> ContextPack:
        block = self._render_flat(keep, show_files) if flat else self._render(keep, show_files)
        n_lines = len([ln for ln in block.splitlines() if ln.strip()])
        levels = max((len(ln) - len(ln.lstrip(" ")) for ln in block.splitlines() if ln), default=0) // 2 + 1
        return ContextPack(
            text=block,
            paths=n_lines,
            chars=len(block),
            est_tokens=int(len(block) / CHARS_PER_TOKEN),
            levels=levels,
            hidden=max(0, self.folder_count - n_lines),
            fits=len(block) <= budget_chars,
            paths_list=sorted(keep),
        )

    def _render_flat(self, keep: set[str], show_files: bool) -> str:
        """Каждая папка — отдельной строкой полным путём (нужно для схем с enum)."""
        lines = []
        for path in sorted(k for k in keep if k):
            node = self.by_path.get(path)
            suffix = f"  ({node.total})" if show_files and node is not None and node.total else ""
            lines.append(f"{path}/{suffix}")
        return "\n".join(lines)

    def section_pack(self, depth: int = 2, budget_chars: int = 9000,
                     show_files: bool = True, hide_garbled: bool = True) -> ContextPack:
        """Карта «разделы» для прохода 1: только верхние ярусы — влезает всегда."""
        pack: ContextPack | None = None
        for d in range(max(1, int(depth)), 0, -1):
            pack = self.pack_from_keep(
                self.keep_up_to(d, hide_garbled=hide_garbled), show_files and d >= 2,
                budget_chars, flat=True)
            pack.notes = [f"скелет до {d} ярусов"] + (["НЕ влезла в бюджет"] if not pack.fits else [])
            if pack.fits:
                return pack
        return pack  # самый скромный вариант (1 ярус)

    def full_map(self, budget_chars: int = 26000, show_files: bool = True,
                 hide_garbled: bool = True, skip_empty: bool = False) -> ContextPack:
        """ВСЁ дерево целиком (цепочки склеены) — ~9k токенов на ~1400 папок.

        skip_empty=True отсекает ветки без единого файла — пустые оболочки не должны
        выглядеть как готовые места назначения."""
        keep = {p for p in self.by_path if p and not self.excluded(p)}
        if skip_empty:
            keep = {p for p in keep if (self.by_path[p].total or 0) > 0}
        if hide_garbled:
            keep = {p for p in keep if not is_garbled(p.rpartition("/")[2])}
        pack = self.pack_from_keep(keep, show_files, budget_chars, flat=False)
        pack.notes = ["полная карта (все ярусы)"] + (["НЕ влезла в бюджет"] if not pack.fits else [])
        return pack

    def subtree_pack(self, sections: Iterable[str], depth: int = 9, budget_chars: int = 20000,
                     show_files: bool = True, anchor: str = "",
                     anchor_depth: int = 1) -> ContextPack:
        """Карта «место» для прохода 2: полное поддерево выбранных разделов.

        Если поддерево гиганта не влезает даже на глубине 2 — помечаем fits=False:
        вызывающий код зумит разбор вглубь (просит модель сузить раздел)."""
        sections = [s for s in sections if s]
        pack: ContextPack | None = None
        for d in range(max(2, int(depth)), 1, -1):   # хотя бы одна попытка даже при depth<=1
            keep = self.subtree_keep(sections, depth=d - 1, anchor=anchor,
                                     anchor_depth=anchor_depth, show_other_roots=True)
            pack = self.pack_from_keep(keep, show_files, budget_chars)
            pack.notes = [f"поддерево выбранных разделов до {d} ярусов от их корня"]
            if pack.fits:
                return pack
        if pack is None or not sections:  # разделов нет — возвращаем хотя бы корни
            pack = self.pack_from_keep(self.keep_up_to(1), False, budget_chars)
            pack.notes = ["раздел не выбран — только корни"]
        else:
            pack.fits = False
            pack.notes.append("поддерево не влезло целиком — нужен зум вглубь")
        return pack

    # ------------------------------------------------------------ helper views
    def tree_lines(self, max_depth: int = 4, limit: int = 5000) -> list[str]:
        out: list[str] = []

        def walk(node: Node, indent: int) -> None:
            if indent > max_depth or len(out) >= limit:
                return
            for child in node.children.values():
                total = f"  ({child.total})" if child.total else ""
                out.append(f"{'  ' * indent}{child.name}/{total}")
                walk(child, indent + 1)

        walk(self.root, 0)
        return out

    def all_paths(self) -> list[str]:
        return sorted(p for p in self.by_path if p)

    def find_existing(self, parent_rel: str, name: str) -> str:
        """Есть ли уже такая подпапка (оценка по весу совпавших токенов) — чтобы
        не плодить двойники и не цепляться к первому попавшемуся соседству."""
        parent = self.by_path.get((parent_rel or "").strip("/"))
        if parent is None or not name:
            return ""
        want = [stem(t) for t in tokens(name)]
        if not want:
            return ""
        best, best_score = "", 0
        for child in parent.children.values():
            have = [stem(t) for t in tokens(child.name)]
            if not have:
                continue
            ws, hs = set(want), set(have)
            score = sum(3 + min(3, len(w)) // 2 for w in ws & hs)   # точные совпадения
            for w in want:                                           # слабые — за префикс
                if w in hs:
                    continue
                if any(w.startswith(h[:4]) or h.startswith(w[:4]) for h in have):
                    score += 1
            if score > best_score:
                best, best_score = child.path, score
        return best if best_score >= 3 else ""


def curated_root_sections(index: "FolderIndex", config: dict) -> list[str]:
    """Реальные разделы первого уровня, в которые модель вправе класть напрямую.

    Помимо детей mo_target_prefix (это top_folders) отдел может иметь соседние
    корневые папки («По собору…», «Личное - другие проекты», «Направления…»,
    «Рекомендуем медиа ресурсы» …). Убираем сам префикс, скрытые ветви разбора и
    «битые» имена — остаётся честный список доступных корней.
    """
    prefix = str(config.get("mo_target_prefix", "")).strip("/").casefold()
    hide = {str(p).replace("\\", "/").strip("/").split("/")[0].casefold()
            for p in config.get("folders", {}).get("hide_prefixes", [])}
    dump = {"разное", "прочее", "новое", "без категории", "старое"}
    out: list[str] = []
    for name in sorted(index.top_levels):
        low = name.casefold()
        if not name or low == prefix or low in hide or low in dump or is_garbled(name):
            continue
        out.append(name)
    return out

"""Оркестрация разбора: один файл — один изолированный запрос к модели.

Здесь живёт адаптивный бюджет контекста. Если Ollama сообщила, что prompt_eval
съедает больше 85% num_ctx, следующий срез папок строится меньше; после серии
успехов бюджет постепенно возвращается. Так программа держится в рамках памяти
видеокарты без ручного подбора чисел.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import extract as extract_mod
from .extract import Extracted
from .folders import FolderIndex, keywords_from_text
from .llm import ContextOverflow, LlmError, OllamaClient, candidate_urls, probe_endpoint
from .prompts import (RESPONSE_SCHEMA, SECTION_SYSTEM, build_section_user, build_system,
                      build_user, section_schema)
from .rules import build_record

APP_DIR = Path(__file__).resolve().parent.parent

TEXT_CAPABLE = {".docx", ".xlsx", ".xlsm", ".pptx", ".pdf", ".txt", ".md", ".csv", ".tsv", ".html", ".htm", ".json", ".xml", ".log"}


@dataclass
class Filters:
    prefix: str = ""
    limit: int = 20
    extensions: set[str] | None = None
    skip_done: bool = True
    include_media: bool = False
    order: str = "path"      # path | size | modified | shallow
    offset: int = 0


class Analyzer:
    def __init__(
        self,
        config: dict,
        snapshot: dict,
        index: FolderIndex,
        log: Callable[[str], None] | None = None,
        on_record: Callable[[dict], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ):
        self.config = config
        self.snapshot = snapshot
        self.index = index
        self.root: Path = config["scan_root"]
        self.output: Path = config["output_path"]
        self.log = log or (lambda msg: None)
        self.on_record = on_record
        self.on_progress = on_progress
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()

        llm_cfg = config.get("llm", {})
        shared = dict(
            base_url=llm_cfg.get("base_url", "http://127.0.0.1:11434"),
            num_ctx=int(llm_cfg.get("num_ctx", 8192)),
            num_predict=int(llm_cfg.get("num_predict", 420)),
            temperature=float(llm_cfg.get("temperature", 0.1)),
            top_p=float(llm_cfg.get("top_p", 0.9)),
            keep_alive=str(llm_cfg.get("keep_alive", "30m")),
            timeout=int(llm_cfg.get("timeout_seconds", 600)),
            cache_policy=llm_cfg.get("cache_policy", "nonce"),
            structured=bool(llm_cfg.get("structured_output", True)),
            retries=int(llm_cfg.get("retries", 2)),
            enable_thinking=bool(llm_cfg.get("enable_thinking", False)),
        )
        self.client = OllamaClient(model=llm_cfg.get("model_text", "gemma3:12b"), **shared)
        self.vision_enabled = bool(llm_cfg.get("vision", False))
        if self.vision_enabled:
            vis_ctx = min(int(shared["num_ctx"]), int(llm_cfg.get("vision_num_ctx", 16384)))
            vis_shared = {**shared, "num_ctx": vis_ctx}
            self.vision_client = OllamaClient(
                model=llm_cfg.get("model_vision", llm_cfg.get("model_text", "gemma3:12b")), **vis_shared)
        else:
            self.vision_client = None
        from . import plugins as _plugins

        _plugins.set_config(config)          # явный путь к tesseract из настроек работает везде
        from .folders import curated_root_sections
        config["top_level_roots"] = curated_root_sections(self.index, config)
        self.system_prompt = build_system(config)
        folders_cfg = config.get("folders", {})
        self.budget_full = int(folders_cfg.get("budget_chars", 7000))
        self.num_ctx = shared["num_ctx"]
        from .config_loader import slice_budget

        self.budget_chars = min(self.budget_full, slice_budget(config, len(self.system_prompt)))
        self.budget_floor = int(folders_cfg.get("budget_min_chars", 1800))
        self.context_note = ""
        if self.budget_chars < self.budget_full:
            self.context_note = (
                f"бюджет карты ужат с {self.budget_full} до {self.budget_chars} симв.: "
                f"инструкция занимает ~{estimate_tokens(self.system_prompt)} ток. из num_ctx={self.num_ctx}"
            )
        self.max_paths = int(folders_cfg.get("max_paths", 300))
        self.depth = int(folders_cfg.get("depth", 4))
        # двухпроходный разбор: проход 1 выбирает раздел по скелету, проход 2 —
        # точное место в полном поддереве выбранного раздела (покрывает любую глубину)
        self.hier = bool(folders_cfg.get("hierarchical", False))
        self.hier_skel_depth = int(folders_cfg.get("hier_skeleton_depth", 2))
        self.hier_sample_chars = int(folders_cfg.get("hier_sample_chars", 1600))
        self.hier_min_conf = float(folders_cfg.get("hier_min_confidence", 0.55))
        self.hier_subtree_depth = int(folders_cfg.get("hier_subtree_depth", folders_cfg.get("depth", 9)))
        self.hier_zooms = int(folders_cfg.get("hier_zoom_passes", 2))
        self.hier_full_map = bool(folders_cfg.get("hier_full_map", False))
        self._last_sec_conf = 0.0
        self._hash_seen: dict[str, dict] = {}
        self._placed: dict[str, str] = {}
        self.rules_first = bool(config.get("queue", {}).get("rules_first", True))
        self._hist_hashes = self._history_answers()
        try:
            from . import fewshot
            fewshot.refresh(config)
        except Exception:
            pass
        self.content_chars = int(llm_cfg.get("content_chars", 3200))
        self.vision_max_side = int(llm_cfg.get("vision_max_side", 1024))
        self.ocr_enabled = bool(config.get("plugins", {}).get("ocr"))
        self.media_meta = bool(config.get("plugins", {}).get("media_info"))
        self.shrink_streak = 0
        self._last_fit = ""
        self.records: list[dict] = []
        self.stale_missing: list[dict] = []
        self.duplicates = self._duplicate_map()
        self.raw_dir = self.output / "llm_raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ setup
    def _history_answers(self) -> dict[str, dict]:
        """sha256 → ответ, восстановленный из прошлых записей suggestions.json."""
        out: dict[str, dict] = {}
        target = self.output / "suggestions.json"
        try:
            rows = json.loads(target.read_text("utf-8")) if target.exists() else []
        except Exception:
            return out
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                digest = str(row.get("_хеш") or "")
                if not digest or row.get("_окончено") is False:
                    continue
                ev = str(row.get("_дата-события") or "")
                start, _, end = ev.partition("…")
                out[digest] = {
                    "doc_type": str(row.get("_тип_документа") or ""),
                    "direction": str(row.get("_направление") or ""),
                    "reg_number": str(row.get("_номер") or ""),
                    "doc_date": str(row.get("_дата") or ""),
                    "event_name": str(row.get("_событие") or ""),
                    "event_start": start, "event_end": end,
                    "new_name": str(row.get("Новое имя") or ""),
                    "target_path": str(row.get("Целевой путь") or ""),
                    "shortcut_to": str(row.get("Путь ярлыка") or ""),
                    "name_confidence": float(row.get("Уверенность имени") or 0),
                    "move_confidence": float(row.get("Уверенность переноса") or 0),
                    "confidence": 0, "review": str(row.get("Статус")) == "проверка",
                    "discard": bool(row.get("_выбросить")),
                    "discard_reason": "", "reason": str(row.get("Причина") or "")[:300],
                }
            except (TypeError, ValueError):
                continue   # одна битая строка не должна ронять весь разбор
        return out

    def _duplicate_map(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for item in self.snapshot.get("files", []):
            digest = str(item.get("sha256") or "")
            if digest:
                groups.setdefault(digest, []).append(str(item.get("path", "")))
        return {d: paths for d, paths in groups.items() if len(paths) > 1}

    def queue(self, filters: Filters) -> list[dict]:
        done = self._processed_paths() if filters.skip_done else {}
        mode = str(self.config.get("queue", {}).get("mode", "завалы")).strip().lower()
        hidden = [str(p).replace("\\", "/").strip("/").casefold()
                  for p in self.config.get("folders", {}).get("hide_prefixes", [])]
        revision = mode.startswith("ревиз") or mode.startswith("структ")
        intake_only = mode.startswith("завал") or mode.startswith("разбор")
        system_first = mode.startswith("система") or mode.startswith("сначала") or "потом" in mode
        items, tail = [], []
        for item in self.snapshot.get("files", []):
            rel = str(item.get("path", "")).replace("\\", "/")
            ext = Path(rel).suffix.lower()
            low_rel = rel.casefold()
            is_intake = any(low_rel == pref or low_rel.startswith(pref + "/") for pref in hidden)
            if revision and is_intake:
                continue
            if intake_only and not filters.prefix and not is_intake:
                continue
            if filters.prefix and not rel.casefold().startswith(filters.prefix.casefold()):
                continue
            if ext == ".py" or rel.casefold().startswith("_"):
                continue
            if filters.extensions and ext not in filters.extensions:
                continue
            if not filters.include_media and ext in extract_mod.IMAGE_EXT:
                continue
            if not filters.include_media and ext not in TEXT_CAPABLE:
                continue
            if filters.skip_done and rel in done:
                saved = done[rel]
                if not saved or saved == str(item.get("sha256") or ""):
                    continue  # тот же файл; пустой saved — старая карта без хеша
            (tail if (system_first and is_intake) else items).append(item)

        def sort_in_place(collection: list[dict]) -> None:
            if filters.order == "size":
                collection.sort(key=lambda i: -int(i.get("size") or 0))
            elif filters.order == "modified":
                collection.sort(key=lambda i: str(i.get("modified", "")), reverse=True)
            elif filters.order == "shallow":
                collection.sort(key=lambda i: (str(i.get("path", "")).count("/"), str(i.get("path", ""))))
            else:
                collection.sort(key=lambda i: str(i.get("path", "")))

        sort_in_place(items)
        if system_first:
            sort_in_place(tail)
            items = items + tail
        if filters.offset:
            items = items[filters.offset:]
        items = items[: filters.limit] if filters.limit else items
        live, missing = [], []
        for item in items:
            rel = str(item.get("path", "")).replace("\\", "/")
            (live if (self.root / rel).exists() else missing).append(item)
        self.stale_missing = missing
        return live

    def _processed_paths(self) -> dict[str, str]:
        """Уже разобранные пути → sha256 (пусто = не знаю; тогда пропускаем как раньше)."""
        paths: dict[str, str] = {}
        for name in ("suggestions.json", "ollama_suggestions.json"):
            target = self.output / name
            if target.exists():
                try:
                    for row in json.loads(target.read_text("utf-8")):
                        if row.get("_окончено") is not False:
                            paths[str(row.get("Текущий путь", ""))] = str(row.get("_хеш") or "")
                except Exception:
                    pass
        import csv
        map_path = self.output / "migration_map.csv"
        if map_path.exists():
            try:
                with map_path.open(encoding="utf-8-sig", newline="") as stream:
                    for row in csv.DictReader(stream, delimiter=";"):
                        if (row.get("Статус") or "").strip() in {"готово", "проверка", "перенесено", "без изменений"}:
                            paths.setdefault((row.get("Текущий путь") or "").replace("\\", "/"), "")
            except Exception:
                pass
        applied = self.output / "migration_map_applied.csv"   # закрытые: перенесённые и «без изменений»
        if applied.exists():
            try:
                with applied.open(encoding="utf-8-sig", newline="") as stream:
                    for row in csv.DictReader(stream, delimiter=";"):
                        paths[(row.get("Текущий путь") or "").replace("\\", "/")] = ""
            except Exception:
                pass
        return paths

    # ------------------------------------------------------------------ vision
    def _image_payload(self, path: Path, max_side: int = 0) -> str:
        return image_payload(path, int(max_side or self.vision_max_side))

    # -------------------------------------------------------------- adaptive
    def _adapt(self, prompt_tokens: int, overflow: bool) -> None:
        used = prompt_tokens / max(1, self.num_ctx)
        if overflow or used > 0.92:
            self.budget_chars = max(self.budget_floor, int(self.budget_chars * 0.8))
            self.shrink_streak += 1
            self.log(f"контекст велик ({prompt_tokens} токенов из {self.num_ctx}) → бюджет среза папок ↓ {self.budget_chars} симв.")
        elif used < 0.6 and self.budget_chars < self.budget_full:
            self.budget_chars = min(self.budget_full, int(self.budget_chars * 1.15) + 400)

    def _fit(self, content: str, pack, anchor: str, keywords: list[str], ceiling_ctx: int | None = None,
             rebuild: Callable[[int], object] | None = None) -> tuple[str, object, str]:
        """Не даст промпту перерасти num_ctx: Ollama тихо отрезает начало, и инструкция «исчезает».

        Сначала честно перестраиваем карту папок меньшего яруса, и только потом — текст файла.
        ceiling_ctx — окно для ЭТОГО запроса (vision-клиент живёт на меньшем num_ctx).
        rebuild — как перестраивать карту (в двухпроходном режиме — поддерево раздела).
        """
        ceiling = int(ceiling_ctx or self.num_ctx)
        ceiling = int(ceiling * 0.94)
        overhead = estimate_tokens(self.system_prompt) + 700
        notes: list[str] = []
        budget = max(1, len(pack.text))
        for _ in range(8):
            if overhead + estimate_tokens(pack.text) + estimate_tokens(content) <= ceiling:
                break
            if budget > self.budget_floor:
                budget = max(self.budget_floor, int(budget * 0.75))
                smaller = (rebuild(budget) if rebuild else self.index.build(
                    anchor=anchor, keywords=keywords, skeleton_depth=self.depth,
                    anchor_depth=self.depth, max_paths=self.max_paths, budget_chars=budget))
                notes.append(f"карта сжата до {len(smaller.text)} симв.")
                if len(smaller.text) >= len(pack.text):
                    break
                pack = smaller
                continue
            if len(content) > 1200:
                content = content[: int(len(content) * 0.7)] + "\n…(текст файла ужат, чтобы инструкция осталась целой)"
                notes.append("текст файла ужат ради целой инструкции")
                continue
            notes.append("тесно даже после сжатия — увеличьте num_ctx")
            break
        total = overhead + estimate_tokens(pack.text) + estimate_tokens(content)
        if total > ceiling:
            notes.append(f"риск: ~{total} ток. при окне {ceiling + 1:.0f}")
        return content, pack, "; ".join(dict.fromkeys(notes))

    # ------------------------------------------------------------------- loop
    def preflight(self) -> None:
        clients = [self.client] + ([self.vision_client] if self.vision_client else [])
        url, alive = probe_endpoint(self.client.base_url)
        if not alive:
            raise LlmError(
                f"Ollama не отвечает (проверены {', '.join(candidate_urls(self.client.base_url))}). "
                "Запустите приложение Ollama."
            )
        for client in clients:
            client.base_url = url
        models = self.client.list_models()
        for client in clients:
            if client.model not in models:
                raise LlmError(f"модель {client.model} не найдена. Доступны: {', '.join(models) or 'нет'}")
        self.log(f"Ollama: {url}; модель {self.client.model}; num_ctx={self.num_ctx}; очистка кэша: {self.client.cache_policy}")
        self.log(f"контекст: инструкция ~{estimate_tokens(self.system_prompt)} ток. "
                 f"({len(self.system_prompt)} симв.), карта ≤{self.budget_chars} симв. "
                 f"(~{estimate_tokens(' ' * self.budget_chars)} ток.), файл ≤{self.content_chars} симв., "
                 f"ответ ≤{self.config.get('llm', {}).get('num_predict', 800)} ток.")
        if self.context_note:
            self.log("  " + self.context_note)
        if self.hier:
            self.log(f"двухпроходный разбор: раздел (скелет ≤{self.hier_skel_depth} ярусов, срез "
                     f"{self.hier_sample_chars} симв.) → поддерево раздела ≤{self.hier_subtree_depth} "
                     f"ярусов, зум до {self.hier_zooms}×; ~2 запроса на файл")

    def run(self, filters: Filters) -> list[dict]:
        self.preflight()
        items = self.queue(filters)
        if self.stale_missing:
            self.log(f"ВНИМАНИЕ: {len(self.stale_missing)} файл(ов) из снимка уже нет на месте "
                     f"(пример: {self.stale_missing[0]['path']}). Снимок устарел — обновите его пунктом «Скан».")
        total = len(items)
        self.log(f"К разбору: {total} файл(ов). Модель: {self.client.model}, num_ctx={self.num_ctx}, кэш: {self.client.cache_policy}.")
        self.records = []
        for position, item in enumerate(items, 1):
            if self.stop_flag.is_set():
                self.log("остановлено по команде оператора")
                break
            while self.pause_flag.is_set() and not self.stop_flag.is_set():
                time.sleep(0.25)
            rel = str(item.get("path", "")).replace("\\", "/")
            self.on_progress and self.on_progress(position - 1, total)
            digest = str(item.get("sha256") or "")
            cached = self._hash_seen.get(digest) if digest else None
            inherited = bool(cached and cached.get("answer"))
            if inherited:
                record = self._inherit(cached, item)
            else:
                record = self.process_one(item)
            self._note_collisions(record, rel)
            self.records.append(record)
            self._persist()
            self.on_record and self.on_record(record)
            self.log(f"[{position}/{total}] {rel} → {record['Целевой путь'] or '?'}/{record['Новое имя'] or '?'} "
                     f"({'копия по хешу' if inherited else ('проверка' if record['Статус'] == 'проверка' else 'готово')}, "
                     f"{record['_время_запроса']}с)")
            self.on_progress and self.on_progress(position, total)
        return self.records

    # ------------------------------------------------------------- дубли по хешу
    def _inherit(self, cached: dict, item: dict) -> dict:
        """Тот же хеш — то же решение: переиспользуем ответ модели без второго запроса.

        Имя и папка пересчитываются build_record() уже под этот файл (у него свой путь),
        строка получает пометку о наследовании; при конфликте двух копий в один адрес
        вторая уйдёт на проверку (_note_collisions)."""
        rel = str(item.get("path", "")).replace("\\", "/")
        dup = [p for p in self.duplicates.get(str(item.get("sha256") or ""), []) if p != rel]
        usage = {"prompt_tokens": 0, "seconds": 0, "model": f"наследует «{cached['rel']}»"}
        verdict = build_record(item, cached["extracted"], cached["answer"],
                               self.index, self.config, self.root, dup, usage)
        record = verdict.record
        record["_блокировок"] = ""
        record["_контекст"] = "без запроса: копия по хешу"
        record["_ошибка"] = ""
        record["_окончено"] = True
        record["_время_запроса"] = 0.0
        record["_флаги"] = (record["_флаги"] + "; " if record.get("_флаги") else "") + \
            f"тот же хеш, что у «{cached['rel']}» — решение унаследовано"
        record["Причина"] = (record["Причина"] + " | " if record.get("Причина") else "") + \
            f"дубликат по содержимому (первоисточник: {cached['rel']})"
        self._dump_raw(rel, cached["answer"], "", usage, cached["extracted"], verdict)
        return record

    def _note_collisions(self, record: dict, rel: str) -> None:
        """Две кандидатуры в одно «папка+имя» — вторая честно уходит на проверку."""
        target = str(record.get("Целевой путь") or "")
        name = str(record.get("Новое имя") or "")
        if not target or not name or str(record.get("Тип операции")) == "в корзину":
            return
        key = f"{target}/{name}".casefold()
        first = self._placed.get(key)
        if first and first != rel:
            record["_флаги"] = (record["_флаги"] + "; " if record.get("_флаги") else "") + \
                f"претендует на то же место и имя, что «{first}» — второму при переносе добавится _v2"
            record["Статус"] = "проверка"
        else:
            self._placed.setdefault(key, rel)

    def process_one(self, item: dict, attempts: int = 3) -> dict:
        rel = str(item.get("path", "")).replace("\\", "/")
        full = self.root / Path(rel)
        started = time.monotonic()
        self._last_fit = ""      # иначе заметка о сжатии «переедет» с прошлого файла
        self._last_sec_conf = 0.0
        digest = str(item.get("sha256") or "")
        if self.rules_first:
            from . import prerecognize
            ans = prerecognize.deterministic(item, self.index, self.config)
            source = "правило"
            if ans is None and digest and digest in self._hist_hashes:
                ans = dict(self._hist_hashes[digest])
                ans["reason"] = ("тот же хеш, что в прежнем разборе: "
                                 + (ans.get("reason") or ""))[:280]
                source = "хеш из карты"
            if ans is not None:
                return self._rule_record(item, ans, source)
        extracted = extract_mod.extract(
            full, self.content_chars, int(self.config.get("llm", {}).get("max_pdf_pages", 12)),
            ocr=self.ocr_enabled, media_meta=self.media_meta,
        )
        anchor = rel.rpartition("/")[0]
        keywords = keywords_from_text(f"{Path(rel).stem} {extracted.text[:900]}")
        duplicates = [p for p in self.duplicates.get(str(item.get("sha256") or ""), []) if p != rel]

        answer: dict = {}
        usage = {"prompt_tokens": 0, "seconds": 0, "model": self.client.model}
        error = ""
        wants_vision = extracted.needs_vision and self.vision_enabled and extracted.kind in ("image", "pdf-scan")
        images = []
        if wants_vision:
            if extracted.kind == "image":
                payload = self._image_payload(full)
                images = [payload] if payload else []
            else:
                images = [img for img in self._pdf_pages_image(full) if img][:1]
        sections: list[str] = []
        section_note = ""
        if not extracted.usable and not images:
            error = extracted.note or "нечего читать"
        else:
            client = self.vision_client if images else self.client
            req_ctx = int(getattr(client, "num_ctx", self.num_ctx))
            from .config_loader import ANSWER_RESERVE_TOKENS, CHARS_PER_TOKEN
            _used = (len(self.system_prompt) + self.content_chars) / CHARS_PER_TOKEN + ANSWER_RESERVE_TOKENS
            req_budget = max(self.budget_floor,
                             min(self.budget_chars, int(max(0, (req_ctx - _used) * CHARS_PER_TOKEN))))
            from . import routing as _routing
            proj = _routing.project_homes(rel, self.index, self.config.get("top_level_roots", []), self.config)
            proj_hint = _routing.hint_text(proj, self.config)
            if self.hier:
                sections, section_note, sec_secs, sec_tok = self._choose_sections(
                    rel, extracted, images, proj_hint, anchor, req_ctx, req_budget)
                usage["seconds"] = round(usage["seconds"] + sec_secs, 1)
                usage["prompt_tokens"] = max(usage["prompt_tokens"], sec_tok)

            boost: list[str] = []
            try:
                from . import embeds
                if embeds.enabled(self.config):
                    boost = embeds.rank_folders(self.config, self.index,
                                                f"{Path(rel).name} {extracted.text[:600]}", k=20)
            except Exception:
                pass

            def build_pack(budget: int):
                if sections:
                    return self.index.subtree_pack(sections, depth=self.hier_subtree_depth,
                                                   budget_chars=budget, anchor=anchor)
                return self.index.build(anchor=anchor, keywords=keywords, skeleton_depth=self.depth,
                                        anchor_depth=self.depth, max_paths=self.max_paths,
                                        budget_chars=budget, boost=boost)

            hint2 = " ".join(s for s in (proj_hint, section_note) if s)
            try:
                from . import fewshot
                fs = fewshot.hints(self.config, rel, extracted.text)
                if fs:
                    hint2 = (hint2 + "\n" if hint2 else "") + fs
            except Exception:
                pass
            for attempt in range(attempts):
                pack = build_pack(req_budget)
                content, pack, fit_note = self._fit(extracted.text, pack, anchor, keywords,
                                                    ceiling_ctx=req_ctx, rebuild=build_pack)
                self._last_fit = fit_note
                if fit_note:
                    self.log("  " + fit_note)
                user = build_user(
                    rel_path=rel,
                    name=Path(rel).name,
                    size=int(item.get("size") or 0),
                    modified=str(item.get("modified", ""))[:19],
                    kind=extracted.kind,
                    note=(extracted.note + ("; " if extracted.note else "") + fit_note) if fit_note else extracted.note,
                    folders_block=pack.text,
                    content=content,
                    hint=hint2,
                    duplicates=duplicates,
                    mode=str(self.config.get("queue", {}).get("mode", "завалы")),
                )
                try:
                    result = client.ask(self.system_prompt, user, RESPONSE_SCHEMA, images=images or None)
                    answer = result.data
                    error = ""   # предыдущая попытка могла быть overflow — успех её аннулирует
                    usage = {"prompt_tokens": result.prompt_tokens, "seconds": round(usage["seconds"] + (result.seconds or 0), 1),
                             "model": result.model}
                    self._adapt(result.prompt_tokens, False)
                    if result.prompt_tokens >= req_ctx * 0.995:
                        self.log(f"  ВНИМАНИЕ: промпт упёрся в окно {req_ctx} ток. ({result.prompt_tokens}) — "
                                 "его мог обрезать Ollama. Уменьшите бюджет карты или увеличьте num_ctx.")
                        if fit_note == "":
                            self._last_fit = f"упёрся в окно {req_ctx} ({result.prompt_tokens})"
                    elif result.prompt_tokens > 0.92 * req_ctx:
                        self.log(f"  внимание: промпт занял {result.prompt_tokens} токенов из {req_ctx}")
                    break
                except ContextOverflow as exc:
                    error = f"контекст переполнен: {exc}"
                    if images:
                        req_budget = max(self.budget_floor, int(req_budget * 0.7))
                    else:
                        self._adapt(int(req_ctx * 0.95), True)
                        # иначе следующая попытка соберёт ровно тот же перерост:
                        req_budget = max(self.budget_floor, min(req_budget, self.budget_chars))
                    self.log(f"  HTTP 500 → сжимаем срез папок, попытка {attempt + 1}/{attempts}")
                except LlmError as exc:
                    error = str(exc)
                    break
                except Exception as exc:  # страховка от неожиданного ответа Ollama
                    error = f"{type(exc).__name__}: {exc}"
                    break

        verdict = build_record(item, extracted, answer, self.index, self.config, self.root, duplicates, usage)
        record = verdict.record
        if sections and record.get("Целевой путь"):
            sec_roots = {s.split("/")[0] for s in sections}
            if record["Целевой путь"].split("/")[0] not in sec_roots:
                record["_флаги"] = (record["_флаги"] + "; " if record["_флаги"] else "") + \
                    "цель вне раздела, выбранного проходом 1 («" + " / ".join(sections) + "») — сверьте оба"
                record["Статус"] = "проверка"
        record["_блокировок"] = ""
        record["_контекст"] = (f"{usage.get('prompt_tokens', 0)} ток. из {self.num_ctx}"
                               + (f"; {self._last_fit}" if self._last_fit else "")
                               + (f"; двухпроходно: {' → '.join(sections)} (раздел {self._last_sec_conf:.2f})"
                                  if sections else ""))
        record["_ошибка"] = error[:300]
        if error:
            record["Статус"] = "проверка"
            record["Причина"] = (record["Причина"] + " | " if record["Причина"] else "") + f"сбой модели: {error[:180]}"
        record["_окончено"] = not error or "переполнен" not in error
        record["_время_запроса"] = record["_время_запроса"] or round(time.monotonic() - started, 1)
        if sections:
            record["_разделы"] = " → ".join(sections)
        digest = str(item.get("sha256") or "")
        if digest and answer:
            self._hash_seen[digest] = {"rel": rel, "answer": answer, "extracted": extracted}
        self._dump_raw(rel, answer, error, usage, extracted, verdict)
        return record

    def _rule_record(self, item: dict, answer: dict, source: str) -> dict:
        """Запись без обращения к модели: правило или хеш из прошлой карты."""
        rel = str(item.get("path", "")).replace("\\", "/")
        dup = [p for p in self.duplicates.get(str(item.get("sha256") or ""), []) if p != rel]
        if source == "хеш из карты":
            extracted = extract_mod.extract(self.root / Path(rel), self.content_chars, 4,
                                            ocr=False, media_meta=False)
        else:
            extracted = Extracted(text="", kind="правило")
        usage = {"prompt_tokens": 0, "seconds": 0, "model": source}
        verdict = build_record(item, extracted, answer, self.index, self.config,
                               self.root, dup, usage)
        record = verdict.record
        record["_блокировок"] = ""
        record["_контекст"] = f"без запроса: {source}"
        record["_ошибка"] = ""
        record["_окончено"] = True
        record["_время_запроса"] = 0.0
        digest = str(item.get("sha256") or "")
        if digest:
            self._hash_seen[digest] = {"rel": rel, "answer": answer, "extracted": extracted}
        self._dump_raw(rel, answer, "", usage, extracted, verdict)
        self.log(f"  {source}, без модели: {record['Целевой путь'] or '?'}/{record['Новое имя'] or '?'}")
        return record

    # --------------------------------------------------- двухпроходный выбор раздела
    @staticmethod
    def _match_section(raw: str, allowed: set[str], index=None, prefix: str = "") -> str:
        val = (raw or "").replace("\\", "/").strip().strip("/")
        if not val:
            return ""
        if val in allowed:
            return val
        fold_map = {s.casefold().replace("ё", "е"): s for s in allowed}
        hit = fold_map.get(val.casefold().replace("ё", "е"), "")
        if hit:
            return hit
        if index is None:
            return ""
        # модель часто сняла корень: «Отчеты…/Годовые отчеты» без «Документы/»
        tail = "/" + val.casefold().replace("ё", "е")
        suff = [s for s in allowed if s.casefold().replace("ё", "е").endswith(tail)]
        if len(suff) == 1:
            return suff[0]
        # путь мог быть собран из цепочек дерева с мелкой ошибкой написания
        from .rules import Verdict, resolve_path
        tries = [val] + ([f"{prefix}/{val}"] if prefix else [])
        for cand_val in tries:
            cand = resolve_path(cand_val, index, Verdict())
            if cand in allowed:
                return cand
        return ""

    def _choose_sections(self, rel: str, extracted: Extracted, images: list[str],
                         proj_hint: str, anchor: str, req_ctx: int, map_ceiling: int):
        """Проход 1: по скелету разделов модель выбирает ветку; гиганта зумим внутрь.

        Возвращает (sections, подсказка_для_прохода_2, секунды, макс_токенов_промпта).
        Пустой sections — тихий откат к обычному однопроходному срезу (не ошибка)."""
        from .config_loader import ANSWER_RESERVE_TOKENS, CHARS_PER_TOKEN
        sample = (extracted.text or "")[: self.hier_sample_chars]
        room = int(max(0, req_ctx * 0.94 - (len(SECTION_SYSTEM) + len(sample)) / CHARS_PER_TOKEN
                       - ANSWER_RESERVE_TOKENS) * CHARS_PER_TOKEN)
        map_budget = max(1500, min(9000, room, max(1500, map_ceiling)))
        client = self.vision_client if images else self.client
        mode = str(self.config.get("queue", {}).get("mode", "завалы"))
        secs: list[str] = []
        note = ""
        seconds = 0.0
        peak_tokens = 0
        for round_ in range(1 + max(0, self.hier_zooms)):
            schema = None
            if round_ == 0:
                if self.hier_full_map:
                    # вся карта деревом (~9k токенов): видны глубокие папки сразу;
                    # enum на 1400 путях раздувает запрос — оставляем схему ключей
                    # (имена полей гарантирует грамма́тика Ollama)
                    pack = self.index.full_map(budget_chars=max(map_budget, 16000))
                    schema = section_schema([])
                else:
                    pack = self.index.section_pack(depth=self.hier_skel_depth, budget_chars=map_budget)
                    schema = section_schema(pack.paths_list)
                extra = ""
            else:
                # поддерево на 2 яруса внутрь выбранных разделов — только его варианты и предлагаются
                keep = self.index.subtree_keep(secs, depth=self.hier_skel_depth,
                                               show_other_roots=False, anchor="")
                inside_keep = {p for p in keep if any(p == s or p.startswith(s + "/") for s in secs)}
                if not inside_keep:
                    break
                pack = self.index.pack_from_keep(inside_keep, True, map_budget, flat=True)
                schema = section_schema(pack.paths_list)
                extra = (f"\n\nРанее выбран раздел «{secs[0]}», но его поддерево не влезает в контекст. "
                         "Выбери ПОДРАЗДЕЛ внутри него (или сам раздел, если он уже достаточно узок).")
            allowed = set(pack.paths_list)
            user = build_section_user(
                rel_path=rel, name=Path(rel).name, kind=extracted.kind,
                note=extracted.note, sections_block=pack.text, content=sample,
                hint=" ".join(s for s in (proj_hint, extra) if s), mode=mode,
                app_dir=Path(self.config.get("app_dir", APP_DIR)))
            try:
                res = client.ask(SECTION_SYSTEM, user, schema, images=images or None)
            except ContextOverflow:
                map_budget = max(1500, int(map_budget * 0.6))
                self.log("  проход «раздел»: контекст тесен — ужимаю скелет")
                continue
            except Exception as exc:
                self.log(f"  проход «раздел» сорвался ({type(exc).__name__}: {str(exc)[:120]}) — работаю в один проход")
                break
            seconds += res.seconds or 0.0
            peak_tokens = max(peak_tokens, res.prompt_tokens or 0)
            data = res.data or {}
            got = self._match_section(str(data.get("section", "")), allowed, self.index, str(self.config.get("mo_target_prefix", "")))
            try:
                conf = float(data.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if round_ == 0:
                if not got:
                    self.log("  проход «раздел»: ответ вне карты — работаю в один проход")
                    break
                alt = self._match_section(str(data.get("alt_section", "")), allowed, self.index, str(self.config.get("mo_target_prefix", "")))
                secs = [got]
                if alt and alt != got and conf < self.hier_min_conf:
                    secs.append(alt)
                self._last_sec_conf = conf
                note = (f"Проход 1: раздел «{got}» (уверенность {conf:.2f}"
                        + (f", альтернатива «{alt}»" if len(secs) > 1 else "")
                        + "). В карте — его поддерево; выбери точную конечную папку и имя. "
                        "Если материал явно из другого раздела карты — выбери его и снизь move_confidence.")
                self.log(f"  проход «раздел»: {got}" + (f" / {alt}" if len(secs) > 1 else "")
                         + f" ({conf:.2f}, {res.seconds}с)")
            elif got:
                secs = [got]
                self._last_sec_conf = conf
                note = (f"Проход 1: внутри «{secs[0]}» выбран подраздел (уверенность {conf:.2f}). "
                        "В карте — его поддерево; выбери точную конечную папку и имя. "
                        "Если материал явно из другого раздела карты — выбери его и снизь move_confidence.")
                self.log(f"  зум раздела: {got} ({conf:.2f}, {res.seconds}с)")
            probe = self.index.subtree_pack(secs, depth=self.hier_subtree_depth,
                                            budget_chars=map_ceiling, anchor=anchor)
            if probe.fits:
                break
        return secs, note, round(seconds, 1), peak_tokens

    def _pdf_pages_image(self, path: Path, max_side: int = 1024) -> list[str]:
        try:
            import fitz

            out = []
            with fitz.open(path) as doc:
                for page in list(doc)[:1]:
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6))
                    out.append(base64.b64encode(pix.tobytes("jpeg")).decode("ascii"))
            return out
        except Exception:
            return []

    # --------------------------------------------------------------- persist
    def _dump_raw(self, rel: str, answer: dict, error: str, usage: dict, extracted: Extracted, verdict) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        safe = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in rel)[-120:]
        payload = {
            "файл": rel,
            "время": stamp,
            "модель": usage.get("model"),
            "токены_промпта": usage.get("prompt_tokens"),
            "секунд": usage.get("seconds"),
            "вид_содержимого": extracted.kind,
            "прочитано_символов": len(extracted.text),
            "флаги": verdict.flags,
            "исправления": verdict.fixed,
            "ошибка": error,
            "ответ": answer,
        }
        try:
            (self.raw_dir / f"{safe}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            pass

    def _persist(self) -> None:
        target = self.output / "suggestions.json"
        merged: dict[str, dict] = {}
        if target.exists():
            try:
                rows = json.loads(target.read_text("utf-8"))
                for row in (rows if isinstance(rows, list) else []):
                    if isinstance(row, dict):
                        merged[str(row.get("Текущий путь"))] = row
            except Exception:
                # молча потерять всю историю хешей хуже, одна лишняя копия — не беда
                try:
                    corrupt = target.with_name(
                        f"suggestions.bad-{datetime.now():%Y%m%d-%H%M%S}.json")
                    target.replace(corrupt)
                    self.log(f"suggestions.json был нечитаем — сохранена копия {corrupt.name}")
                except OSError:
                    pass
                merged = {}
        for row in self.records:
            merged[str(row.get("Текущий путь"))] = row
        text = json.dumps(list(merged.values()), ensure_ascii=False, indent=2)
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(text, "utf-8")
            tmp.replace(target)
        except OSError as exc:
            self.log(f"не записал suggestions.json ({type(exc).__name__}: {exc}) — "
                     "решения этой пачки остались только в карте")


def image_payload(path: Path, max_side: int = 1024) -> str:
    """Картинка → base64, уменьшенная под vision-окно модели (12 МП JPEG в модель не гоним)."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=82)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        try:
            return base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception:
            return ""


_CACHE: dict[str, tuple[object, object]] = {}


def invalidate_snapshot_cache(output_path) -> None:
    """Забыть кэш снимка+индекса после скана — иначе «свежие» папки считаются выдуманными."""
    _CACHE.pop(str(output_path), None)
    try:
        from . import folders_ops
        folders_ops.invalidate_index_cache()
    except Exception:
        pass


def _snapshot_and_index(config: dict):
    """Снимок и индекс папок один раз на запуск — они тяжёлые."""
    key = str(config["output_path"])
    from . import scanner

    if key not in _CACHE:
        snapshot = scanner.load_snapshot(config)
        if not snapshot:
            raise FileNotFoundError("нет снимка диска — сначала выполните сканирование")
        folders_cfg = config.get("folders", {})
        index = FolderIndex.from_snapshot(
            snapshot,
            prune=[*sorted(config["excluded_dirs"]), config["output_path"].name],
            exclude=folders_cfg.get("hide_prefixes", []),
        )
        _CACHE[key] = (snapshot, index)
    return _CACHE[key]


def context_pack_for(config: dict, rel_path: str, text_sample: str = "", budget_chars: int | None = None):
    """Срез структуры и извлечённое содержимое для одного файла."""
    snapshot, index = _snapshot_and_index(config)
    rel = (rel_path or "").replace("\\", "/")
    item = next((i for i in snapshot.get("files", []) if str(i.get("path", "")).replace("\\", "/") == rel), None)
    from . import extract as extract_mod

    path = Path(config["scan_root"]) / rel
    data = extract_mod.extract(
        path, int(config["llm"].get("content_chars", 3200)),
        int(config["llm"].get("max_pdf_pages", 12)),
        ocr=bool(config.get("plugins", {}).get("ocr")),
        media_meta=bool(config.get("plugins", {}).get("media_info")),
    )
    folders_cfg = config.get("folders", {})
    pack = index.build(
        anchor=rel.rpartition("/")[0],
        keywords=keywords_from_text(f"{Path(rel).stem} {(text_sample or data.text)[:900]}"),
        skeleton_depth=int(folders_cfg.get("depth", 5)),
        anchor_depth=int(folders_cfg.get("depth", 5)),
        search_hits=int(folders_cfg.get("search_hits", 25)),
        max_paths=int(folders_cfg.get("max_paths", 300)),
        budget_chars=int(budget_chars or folders_cfg.get("budget_chars", 7000)),
        show_files=bool(folders_cfg.get("show_file_counts", True)),
        hide_garbled=bool(folders_cfg.get("hide_garbled", True)),
    )
    return item, data, pack, index


def build_prompt_preview(config: dict, rel_path: str) -> str:
    """Ровно тот текст, который ушёл бы в модель по этому файлу."""
    item, data, pack, _ = context_pack_for(config, rel_path)
    user = build_user(
        rel_path=rel_path,
        name=Path(rel_path).name,
        size=int((item or {}).get("size") or 0),
        modified=str((item or {}).get("modified", ""))[:19],
        kind=data.kind,
        note=data.note,
        folders_block=pack.text,
        content=data.text,
        mode=str(config.get("queue", {}).get("mode", "завалы")),
        app_dir=Path(config.get("app_dir", APP_DIR)),
    )
    system = build_system(config, Path(config.get("app_dir", APP_DIR)))
    return (
        f"=== SYSTEM ({len(system)} симв., ~{estimate_tokens(system)} токенов) ===\n{system}\n\n"
        f"=== СРЕЗ ПАПКИ: {pack.summary} ===\n\n"
        f"=== USER ({len(user)} симв., ~{estimate_tokens(user)} токенов) ===\n{user}"
    )


def estimate_tokens(text: str) -> int:
    from .folders import CHARS_PER_TOKEN

    return int(len(text or "") / CHARS_PER_TOKEN)

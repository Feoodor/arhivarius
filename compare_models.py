#!/usr/bin/env python3
"""Сравнение моделей на ВАШИХ файлах: одни и те же файлы разбирает каждая модель, результаты рядом.

    python compare_models.py --models gemma3:4b,qwen3.5:9b,gemma4:26b
    python compare_models.py --models gemma3:4b,qwen3.5:9b --files 12 --folder "Документы/00_Разобрать"

Перед этим один раз выполните сканирование в программе (нужен снимок архива). Ничего не
переименовывается и не записывается в карту: это только проверка. Файлы берутся из очереди
разбора равномерно по всему архиву, поэтому в выборку попадают разные типы документов.
Результат печатается и сохраняется в ``model_compare.txt``: по нему видно, какая модель точнее
называет файлы и кладёт их в нужные папки, и сколько секунд ей на это нужно.
В файле есть имена ваших файлов — отправляйте его только если это допустимо.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

from core import pipeline                                    # noqa: E402
from core.config_loader import load_config                   # noqa: E402
from core.folders import FolderIndex                         # noqa: E402
from core.llm import LlmError                                # noqa: E402

REPORT_NAME = "model_compare.txt"


def sample_queue(items: list[dict], count: int) -> list[dict]:
    """Равномерная выборка по всей очереди (а не первые N из одной папки)."""
    if count <= 0 or len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def run_model(config: dict, snapshot: dict, index: FolderIndex, model: str, files: list[dict],
              log=print) -> dict:
    """Разобрать файлы одной моделью. Возвращает {'model', 'rows': {путь: запись}, 'error', 'seconds'}."""
    cfg = copy.deepcopy(config)
    cfg["llm"]["model_text"] = model
    cfg["llm"]["model_vision"] = model
    pipeline._CACHE.clear()
    result = {"model": model, "rows": {}, "error": "", "seconds": {}, "used": model}
    analyzer = pipeline.Analyzer(cfg, snapshot, index, log=lambda m: None)
    analyzer.rules_first = False                         # сравниваем модели, а не правила-подсказки
    try:
        analyzer.preflight()
    except LlmError as exc:
        result["error"] = str(exc)
        return result
    result["used"] = analyzer.client.model
    for number, item in enumerate(files, 1):
        started = time.monotonic()
        log(f"   [{model}] {number}/{len(files)} {Path(str(item.get('path'))).name}")
        try:
            record = analyzer.process_one(item)
        except LlmError as exc:
            result["error"] = str(exc)
            break
        key = str(item.get("path", "")).replace("\\", "/")
        result["rows"][key] = record
        result["seconds"][key] = time.monotonic() - started
    try:
        analyzer.client.unload()
    except Exception:
        pass
    return result


def folder_of(record: dict) -> str:
    return str(record.get("Целевой путь") or "").strip("/")


def build_report(files: list[dict], results: list[dict]) -> list[str]:
    lines = ["СРАВНЕНИЕ МОДЕЛЕЙ", "=" * 17, f"файлов в выборке: {len(files)}; моделей: {len(results)}", ""]
    for number, item in enumerate(files, 1):
        key = str(item.get("path", "")).replace("\\", "/")
        lines.append(f"{number}. {key}")
        for res in results:
            record = res["rows"].get(key)
            if record is None:
                lines.append(f"     {res['model']}: — (не разобран{': ' + res['error'][:80] if res['error'] else ''})")
                continue
            status = record.get("Статус") or "?"
            name = record.get("Новое имя") or "—"
            note = ""
            if status != "готово" and record.get("Причина"):
                note = f"  [{str(record['Причина'])[:90]}]"
            lines.append(f"     {res['model']} ({res['seconds'].get(key, 0):.0f} с, {status}): "
                         f"{name}  →  {folder_of(record) or '—'}{note}")
        lines.append("")
    lines += ["ИТОГ", "-" * 4]
    for res in results:
        rows = res["rows"]
        if res["error"] and not rows:
            lines.append(f"  {res['model']}: не запустилась — {res['error'][:150]}")
            continue
        done = sum(1 for r in rows.values() if r.get("Статус") == "готово")
        total = sum(res["seconds"].values())
        avg = total / len(rows) if rows else 0
        extra = f", остановилась: {res['error'][:100]}" if res["error"] else ""
        used = f" (использована {res['used']})" if res["used"] != res["model"] else ""
        lines.append(f"  {res['model']}{used}: разобрано {len(rows)}, уверенно «готово» {done}, "
                     f"в среднем {avg:.0f} с на файл, всего {total / 60:.1f} мин{extra}")
    if len(results) > 1:
        lines.append("\nСОВПАДЕНИЕ ПАПОК между моделями (доля файлов, куда обе положили в одну папку)")
        for i, first in enumerate(results):
            for second in results[i + 1:]:
                common = [k for k in first["rows"] if k in second["rows"]]
                same = sum(1 for k in common if folder_of(first["rows"][k]) == folder_of(second["rows"][k]))
                lines.append(f"  {first['model']} ~ {second['model']}: {same}/{len(common)}")
    lines.append("\nКак читать: совпадение не значит «правильно». Просмотрите список выше и отметьте, "
                 "чьи имена и папки вы приняли бы без правок.")
    return lines


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Сравнение моделей Ollama на файлах архива")
    parser.add_argument("--models", required=True, help="через запятую: gemma3:4b,qwen3.5:9b")
    parser.add_argument("--files", type=int, default=10, help="сколько файлов взять (по умолчанию 10)")
    parser.add_argument("--folder", default="", help="брать файлы только из этой папки архива")
    parser.add_argument("--out", default=str(APP / REPORT_NAME))
    args = parser.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        parser.error("укажите хотя бы одну модель")

    config = load_config(APP)
    snapshot_path = config["output_path"] / "snapshot.json"
    if not snapshot_path.exists():
        print("Нет снимка архива. Откройте программу и выполните сканирование (вкладка «Сканирование»).")
        return 2
    snapshot = json.loads(snapshot_path.read_text("utf-8"))
    index = FolderIndex.from_snapshot(
        snapshot, prune=sorted(config["excluded_dirs"]) + [config["output_path"].name],
        exclude=config["folders"]["hide_prefixes"])
    probe = pipeline.Analyzer(copy.deepcopy(config), snapshot, index, log=lambda m: None)
    queue = probe.queue(pipeline.Filters(prefix=args.folder, limit=100000, skip_done=False,
                                             include_media=True))   # картинки и сканы — для проверки зрения
    files = sample_queue(queue, args.files)
    if not files:
        print("В очереди нет файлов для разбора (проверьте режим очереди и папку).")
        return 2
    print(f"Файлов в выборке: {len(files)} из {len(queue)}. Моделей: {len(models)}. Это может занять долго.")
    results = [run_model(config, snapshot, index, model, files) for model in models]
    text = "\n".join(build_report(files, results)) + "\n"
    print("\n" + text)
    try:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Сохранено: {args.out}")
    except OSError as exc:
        print(f"Файл не сохранён ({exc}); скопируйте текст выше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Эталонная проверка качества: гоняет реальные файлы из etalon.json и сверяет.

    python etalon_check.py            # все случаи
    python etalon_check.py --nav      # только навигатор по папкам

Нужны запущенная Ollama и актуальный снимок. Вывод: точность по случаям и
список расхождений — по нему видно, не сломал ли правка промпта прежние решения.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

from core.config_loader import load_config          # noqa: E402
from core import pipeline, folders_ops              # noqa: E402
from core.folders import FolderIndex, curated_root_sections  # noqa: E402


def main() -> int:
    only_nav = "--nav" in sys.argv
    cfg = load_config(APP)
    snap = json.loads((cfg["output_path"] / "snapshot.json").read_text("utf-8"))
    index = FolderIndex.from_snapshot(
        snap, prune=sorted(cfg["excluded_dirs"]) + [cfg["output_path"].name],
        exclude=cfg["folders"]["hide_prefixes"])
    cfg["top_level_roots"] = curated_root_sections(index, cfg)
    pipeline._CACHE.clear()
    etalon = json.loads((APP / "etalon.json").read_text("utf-8"))
    an = pipeline.Analyzer(cfg, snap, index, log=lambda m: print("  ", m))
    an.rules_first = False  # эталон проверяет именно модель
    ok = bad = skip = 0
    if not only_nav:
        fmap = {i["path"].replace("\\", "/"): i for i in snap["files"]}
        for case in etalon.get("случаи", []):
            item = fmap.get(case["файл"])
            if not item or not (cfg["scan_root"] / case["файл"]).exists():
                skip += 1
                print("⏭  нет файла:", case["файл"])
                continue
            t0 = time.monotonic()
            rec = an.process_one(item)
            got = str(rec.get("Целевой путь") or "")
            good = got.startswith(case["ожидаемый_префикс"])
            ok += good
            bad += not good
            print(("✅" if good else "❌"), f"{time.monotonic()-t0:.0f}с", case["файл"].rpartition('/')[2])
            if not good:
                print("      ждали:", case["ожидаемый_префикс"], "\n      получили:", got)
    if only_nav or not etalon.get("случаи"):
        from core.folder_router import propose_folder, root_name_collisions
        from core.llm import OllamaClient
        llm = cfg["llm"]
        client = OllamaClient(base_url=llm["base_url"], model=llm["model_text"],
                              num_ctx=20480, num_predict=260, temperature=0.1, top_p=0.9,
                              keep_alive=llm.get("keep_alive", "30m"),
                              timeout=1800, cache_policy="nonce", structured=True, retries=1)
        for case in etalon.get("навигатор", []):
            folder = case["папка"]
            base = cfg["scan_root"] / folder
            if not base.is_dir():
                skip += 1
                continue
            dos = {"rel": folder, "total": sum(1 for _ in base.rglob("*") if _.is_file()),
                   "child_folders": sorted(d.name for d in base.iterdir() if d.is_dir())[:20],
                   "child_files": sorted(f.name for f in base.iterdir() if f.is_file())[:25],
                   "sample_n": 25}
            a = propose_folder(cfg, dos, root_name_collisions(cfg, index, folder, list(index.top_levels)),
                               index, client, APP)
            good = str(a.get("section") or "").startswith(case["ожидаемый_префикс"])
            ok += good
            bad += not good
            print(("✅" if good else "❌"), folder, "→", a.get("section") or "-")
    total = ok + bad
    print(f"\nЭталон: {ok}/{total} ({'—' if not total else round(100*ok/total)}%), пропущено {skip}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

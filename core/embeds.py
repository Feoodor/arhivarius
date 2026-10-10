"""Локальные эмбеддинги для поиска папок (опционально, llm.embed_model).

Ключевые слова по стволам слов («слет»/«слёт», «магнитик»/«магнитики») — слабое
место дешёвой логики: синонимы и разные падежи расходятся. Эмбеддинг-модель
(например snowflake-arctic-embed:224m — ~300 МБ, тоже локальная, через Ollama)
считает смысл: «еда на слёте» найдёт «Меню слета». Векторы папок кэшируются в
output/embed_cache.json и пересчитываются только при изменении дерева.

По умолчанию выключено (llm.embed_model = ""). Включается в «Настройках».
"""
from __future__ import annotations

import json
import math
import urllib.request

CACHE_NAME = "embed_cache.json"


def enabled(config) -> bool:
    return bool(str(config.get("llm", {}).get("embed_model", "")).strip())


def _embed_batch(config, texts: list[str]) -> list[list[float]]:
    url = config["llm"]["base_url"].rstrip("/") + "/api/embed"
    payload = {"model": config["llm"]["embed_model"], "input": texts}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    embs = data.get("embeddings") or []
    if len(embs) != len(texts):
        raise RuntimeError("эмбеддинги вернулись не по числу строк")
    return embs


def _load_cache(config) -> dict:
    path = config["output_path"] / CACHE_NAME
    try:
        cache = json.loads(path.read_text("utf-8"))
    except Exception:
        cache = {}
    if cache.get("model") != config["llm"]["embed_model"]:
        cache = {"model": config["llm"]["embed_model"], "vectors": {}}
    cache.setdefault("vectors", {})
    return cache


def _save_cache(config, cache) -> None:
    try:
        import os

        target = config["output_path"] / CACHE_NAME
        tmp = target.with_name(CACHE_NAME + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def vectors_for(config, paths: list[str]) -> dict[str, list[float]]:
    cache = _load_cache(config)
    vecs: dict[str, list[float]] = cache["vectors"]
    missing = [p for p in paths if p not in vecs]
    for i in range(0, len(missing), 64):
        chunk = missing[i:i + 64]
        try:
            embs = _embed_batch(config, chunk)
        except Exception:
            return {p: vecs[p] for p in paths if p in vecs}
        vecs.update(dict(zip(chunk, embs)))
    cache["vectors"] = {p: vecs[p] for p in paths}  # чистим ушедшие папки
    _save_cache(config, cache)
    return {p: vecs[p] for p in paths if p in vecs}


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def rank_folders(config, index, query: str, k: int = 30) -> list[str]:
    """Топ-k папок по смысловой близости к тексту запроса ('' → пусто)."""
    if not enabled(config) or not (query or "").strip():
        return []
    paths = [p for p in index.by_path if p and not index.excluded(p)]
    if not paths:
        return []
    try:
        folder_vecs = vectors_for(config, paths)
        qv = _embed_batch(config, [query[:2000]])[0]
    except Exception:
        return []
    scored = sorted((( _cos(qv, folder_vecs[p]), p) for p in paths if p in folder_vecs),
                    key=lambda t: -t[0])
    return [p for _, p in scored[:k]]

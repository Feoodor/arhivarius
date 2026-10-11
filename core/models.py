"""Подбор моделей Ollama: что установлено, что подойдёт вместо отсутствующей, что скачать.

Программа не должна падать оттого, что в настройках написана модель, которой на
этом компьютере нет (другой ПК, другой тег, ``gemma3`` вместо ``gemma3:12b``).
Здесь три вещи:

* ``pick_best`` — лучшая из УСТАНОВЛЕННЫХ моделей под задачу (текст или зрение);
* ``plan_models`` — какие модели использовать в этом прогоне и почему (с
  понятным объяснением для журнала), без записи в config.json;
* ``RECOMMENDED`` — короткий список «что скачать», с пояснением по железу.

Ранжирование — по семействам, проверенным на русском тексте со структурным
JSON-ответом. Порядок можно менять: это только запасной выбор, настройки
пользователя главнее.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm import ModelMissing, OllamaClient, OllamaUnavailable, normalize_model, same_model

# от лучшего к худшему; совпадение по началу имени
FAMILY_RANK = (
    "gemma3", "gemma4", "qwen3-vl", "mistral-small3", "qwen2.5vl", "llama3.2-vision",
    "qwen3", "qwen2.5", "gemma3n", "llama3.3", "llama3.1", "phi4", "mistral", "gemma2",
)

EMBEDDING_MARKERS = ("embed", "bge-", "nomic", "minilm", "mxbai", "e5-", "gte-", "arctic-embed",
                     "granite-embedding", "paraphrase-multilingual", "reranker")

# модели не для документов: код, математика, защитные фильтры
UNSUITABLE_MARKERS = ("coder", "codellama", "starcoder", "-code", "math", "guard")

# Размер скачанных весов, выше которого модель на обычной видеокарте (12–16 ГБ) уже медленна
COMFORT_BYTES = 17 * 10 ** 9

RECOMMENDED = (
    {"tag": "gemma3:12b", "role": "основная (текст + зрение)", "gb": 8.1,
     "why": "лучший баланс скорости и качества на русском; видеокарта от 10–12 ГБ"},
    {"tag": "gemma3:27b", "role": "максимум качества (текст + зрение)", "gb": 17,
     "why": "точнее с именами и папками; нужна видеокарта от 24 ГБ"},
    {"tag": "gemma3:4b", "role": "слабый компьютер (текст + зрение)", "gb": 3.3,
     "why": "работает на 6 ГБ и без видеокарты, но чаще отправляет на проверку"},
    {"tag": "qwen3-vl:8b", "role": "альтернатива для сканов и фото", "gb": 6.1,
     "why": "сильное чтение текста на картинках (OCR); 8–10 ГБ видеопамяти"},
    {"tag": "bge-m3", "role": "поиск папок по смыслу (эмбеддинги)", "gb": 1.2,
     "why": "необязательно; включается в настройках, помогает находить папку по теме"},
)


def is_embedding(name: str) -> bool:
    low = (name or "").lower()
    return any(marker in low for marker in EMBEDDING_MARKERS)


def is_unsuitable(name: str) -> bool:
    low = (name or "").lower()
    return is_embedding(low) or any(marker in low for marker in UNSUITABLE_MARKERS)


def _param_billions(name: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)b\b", (name or "").lower())
    return float(match.group(1)) if match else 0.0


def _family_rank(name: str) -> int:
    low = (name or "").lower().rsplit("/", 1)[-1]
    # более длинные префиксы проверяем первыми: «gemma3n» не должна считаться «gemma3»
    best = len(FAMILY_RANK)
    for rank, fam in sorted(enumerate(FAMILY_RANK), key=lambda t: -len(t[1])):
        if low.startswith(fam) and (len(low) == len(fam) or not low[len(fam)].isalnum()):
            best = rank
            break
    return best


def pick_best(installed: list[dict], *, vision: bool, client: OllamaClient | None = None,
              exclude: tuple[str, ...] = ()) -> str:
    """Лучшая установленная модель. vision=True — только те, что принимают картинки.

    installed — строки /api/tags ({'name', 'size'}). Возможности спрашиваем у
    client (если он есть); без него полагаемся только на имя семейства."""
    candidates: list[tuple[tuple, str]] = []
    for item in installed:
        name = str(item.get("name", ""))
        if not name or is_unsuitable(name) or any(same_model(name, x) for x in exclude):
            continue
        if client is not None:
            caps = client.capabilities(name)
            if caps and "completion" not in caps:
                continue
            if vision and caps and "vision" not in caps:
                continue
            if vision and not caps and _family_rank(name) >= len(FAMILY_RANK):
                continue                        # о незнакомой модели ничего не известно — не рискуем
        size = int(item.get("size") or 0)
        params = _param_billions(name)
        comfortable = 0 if (size and size > COMFORT_BYTES) else 1
        tiny = 1 if 0 < params < 3 else 0              # модель на 1–2 млрд хуже любой нормальной, даже из «худшего» семейства
        candidates.append(((-comfortable, tiny, _family_rank(name), -params, -size), name))
    candidates.sort()
    return candidates[0][1] if candidates else ""


@dataclass
class ModelPlan:
    text: str = ""
    vision: str = ""
    vision_enabled: bool = True
    notes: list[str] = field(default_factory=list)      # что показать человеку в журнале
    problems: list[str] = field(default_factory=list)   # то, из-за чего работа невозможна

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.problems


def plan_models(client: OllamaClient, want_text: str, want_vision: str, vision_wanted: bool) -> ModelPlan:
    """Решить, какие модели использовать в этом прогоне.

    Установленная модель из настроек берётся как есть. Отсутствующую заменяет
    лучшая установленная — с явной пометкой в notes, настройки не переписываются."""
    plan = ModelPlan()
    details = client.list_models_detailed()
    names = [d["name"] for d in details]
    if not names:
        plan.problems.append("в Ollama нет ни одной модели. Скачайте основную: ollama pull gemma3:12b")
        return plan

    def resolve(want: str) -> str:
        return next((n for n in names if same_model(want, n)), "")

    text = resolve(want_text)
    if not text:
        text = pick_best(details, vision=False, client=client)
        if not text:
            plan.problems.append(f"модель «{want_text}» не найдена, а подходящей замены нет. "
                                 f"Скачайте: ollama pull {want_text}")
            return plan
        plan.notes.append(f"модель «{want_text}» не установлена — на этот запуск взята «{text}». "
                          f"Чтобы использовать выбранную: ollama pull {want_text}")
    plan.text = text

    if not vision_wanted:
        plan.vision_enabled = False
        return plan
    vision = resolve(want_vision or want_text)
    if vision and client.supports_vision(vision) is False:
        plan.notes.append(f"«{vision}» не умеет читать картинки")
        vision = ""
    if not vision:
        alt = (text if client.supports_vision(text) else "") or pick_best(
            details, vision=True, client=client)
        if alt:
            plan.notes.append(f"для картинок и сканов взята «{alt}» (замена «{want_vision or want_text}»)")
            vision = alt
        else:
            plan.vision_enabled = False
            plan.notes.append("зрение отключено на этот запуск: ни одна установленная модель не читает "
                              "картинки. Картинки и сканы уйдут на проверку. Скачайте: ollama pull "
                              "gemma3:12b")
    plan.vision = vision
    return plan


def prepare(client: OllamaClient, *, vision: bool = False, log=None) -> OllamaClient:
    """Подготовить клиент к работе: Ollama жива, модель установлена (иначе — замена), окно в пределах модели.

    Для одиночных действий (перепроверка, «папка целиком», уроки), где нет общего
    preflight конвейера. Бросает OllamaUnavailable / ModelMissing с понятным текстом."""
    log = log or (lambda msg: None)
    if not client.is_alive():
        raise OllamaUnavailable("Ollama не отвечает. Запустите приложение Ollama и повторите.")
    details = client.list_models_detailed()
    names = [d["name"] for d in details]
    exact = next((n for n in names if same_model(client.model, n)), "")
    if exact:
        client.model = exact
    else:
        alt = pick_best(details, vision=vision, client=client)
        if not alt:
            raise ModelMissing(client.model, names)
        log(f"  модель «{client.model}» не установлена — беру «{alt}» "
            f"(скачать выбранную: ollama pull {client.model})")
        client.model = alt
    if vision and client.supports_vision(client.model) is False:
        alt = pick_best(details, vision=True, client=client)
        if not alt:
            raise ModelMissing(client.model, names)
        log(f"  «{client.model}» не читает картинки — для зрения беру «{alt}»")
        client.model = alt
    limit = client.context_limit(client.model)
    if limit and client.num_ctx > limit:
        client.num_ctx = max(4096, limit)
    return client


def missing_recommended(installed_names: list[str]) -> list[dict]:
    """Рекомендованные модели, которых ещё нет (для кнопки «Скачать»)."""
    return [r for r in RECOMMENDED if not any(same_model(r["tag"], n) for n in installed_names)]


__all__ = ["RECOMMENDED", "ModelPlan", "pick_best", "plan_models", "prepare", "missing_recommended",
           "is_embedding", "normalize_model"]

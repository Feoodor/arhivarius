"""Клиент Ollama: один файл — один stateless-запрос.

Два соображения, из-за которых здесь всё аккуратно:

* Контекст. Модель на 12 ГБ VRAM падает с HTTP 500, если в prompt_eval уходит
  30-40 тыс. токенов. Поэтому запрос всегда одноплатёжный (без истории), а
  budget-блоки считает вызывающий код; сюда приходит только готовый текст.
* Чистота ответа. Ollama кэширует KV по префиксу промпта. Чтобы предыдущий
  файл не «подтёкал» в следующий, в system добавляется уникальный nonce
  (policy=nonce) либо модель полностью выгружается перед запросом
  (policy=unload, медленнее: прогреть 8-17 ГБ заново).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 600


def candidate_urls(base_url: str = "") -> list[str]:
    """Где может висеть Ollama: настройка, переменная OLLAMA_HOST, локальные адреса."""
    urls: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if not value:
            return
        if "://" not in value:
            value = "http://" + value
        value = value.rstrip("/")
        if value not in urls:
            urls.append(value)

    add(base_url)
    add(os.environ.get("OLLAMA_HOST", ""))
    add("http://127.0.0.1:11434")
    add("http://localhost:11434")
    return urls


def probe_endpoint(base_url: str = "", timeout: int = 5) -> tuple[str, bool]:
    """Найти отвечающий адрес Ollama. Возвращает (url, жив_ли)."""
    for url in candidate_urls(base_url):
        try:
            with urllib.request.urlopen(f"{url}/api/version", timeout=timeout) as response:
                json.loads(response.read().decode("utf-8"))
            return url, True
        except Exception:
            continue
    return (candidate_urls(base_url) or ["http://127.0.0.1:11434"])[0], False


class LlmError(RuntimeError):
    """Обычная ошибка обращения к модели (пишем в журнал, строка идёт в review)."""


class ContextOverflow(LlmError):
    """Контекст не влез: вызывающий код обязан ужать блоки и повторить."""


@dataclass
class LlmResult:
    data: dict = field(default_factory=dict)
    raw: str = ""
    prompt_tokens: int = 0
    answer_tokens: int = 0
    seconds: float = 0.0
    model: str = ""
    attempts: int = 0


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "gemma3:12b",
        num_ctx: int = 8192,
        num_predict: int = 420,
        temperature: float = 0.1,
        top_p: float = 0.9,
        keep_alive: str = "30m",
        timeout: int = DEFAULT_TIMEOUT,
        cache_policy: str = "nonce",
        structured: bool = True,
        retries: int = 2,
        enable_thinking: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = int(num_ctx)
        self.num_predict = int(num_predict)
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.keep_alive = keep_alive
        self.timeout = int(timeout)
        self.cache_policy = cache_policy if cache_policy in ("none", "nonce", "unload") else "nonce"
        self.structured = structured
        self.retries = max(0, int(retries))
        self.enable_thinking = bool(enable_thinking)
        self.last_unloaded = False

    # ------------------------------------------------------------- introspect
    def _get(self, path: str, timeout: int = 8):
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def is_alive(self) -> bool:
        try:
            self._get("/api/version")
            return True
        except Exception:
            url, alive = probe_endpoint(self.base_url)
            if alive:
                self.base_url = url
            return alive

    def list_models(self) -> list[str]:
        try:
            return sorted(m["name"] for m in self._get("/api/tags").get("models", []))
        except Exception:
            return []

    def loaded(self) -> list[dict]:
        try:
            return self._get("/api/ps").get("models", [])
        except Exception:
            return []

    def unload(self, model: str | None = None) -> None:
        """Сброс KV-кэша: keep_alive=0 выгружает модель из памяти."""
        payload = {"model": model or self.model, "keep_alive": 0}
        try:
            self._post("/api/generate", payload, timeout=60)
            self.last_unloaded = True
        except Exception:
            pass

    # ------------------------------------------------------------------ calls
    def _post(self, path: str, payload: dict, timeout: int | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                body = resp.read()
            try:
                return json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise LlmError(
                    f"Ollama вернула не-JSON ({type(exc).__name__}): {body[:120]!r}") from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            low = detail.lower()
            looks_overflow = any(
                phrase in low for phrase in
                ("context length", "context window", "exceeds", "too long",
                 "increase context", "please increase", "more system memory",
                 "too many images", "maximum context"))
            if looks_overflow:
                raise ContextOverflow(f"HTTP {exc.code}: {detail or exc.reason}") from exc
            if exc.code >= 500:
                # временный сбой сервера — обычная воспроизводимая ошибка, не переполнение
                raise LlmError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
            raise LlmError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise LlmError(f"Ollama недоступна: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LlmError(f"таймаут {self.timeout}s — модель не ответила") from exc

    def ask(self, system: str, user: str, schema: dict | None = None, images: list[str] | None = None) -> LlmResult:
        """Один изолированный запрос. История не ведётся намеренно."""
        prompt_system = system
        if self.cache_policy == "nonce":
            prompt_system = f"{system}\n\n[запрос {uuid.uuid4().hex[:12]} — отвечай только по этому файлу]"
        messages = [{"role": "system", "content": prompt_system}, {"role": "user", "content": user}]
        if images:
            messages[-1]["images"] = images

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        if self.structured and schema:
            payload["format"] = schema
        else:
            payload["format"] = "json"
        if not self.enable_thinking:
            payload["think"] = False

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if self.cache_policy == "unload":
                self.unload()
            started = time.monotonic()
            try:
                response = self._post("/api/chat", payload)
                raw = (response.get("message") or {}).get("content", "")
                result = LlmResult(
                    raw=raw,
                    prompt_tokens=int(response.get("prompt_eval_count") or 0),
                    answer_tokens=int(response.get("eval_count") or 0),
                    seconds=round(time.monotonic() - started, 1),
                    model=self.model,
                    attempts=attempt + 1,
                )
                result.data = _parse_json(raw)
                if not result.data:
                    raise LlmError(f"не разобран ответ: {raw[:160] or '(пусто)'}")
                return result
            except ContextOverflow as exc:
                last_error = exc
                break  # ужатием контекста занимается вызывающий код
            except LlmError as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
        raise last_error if last_error else LlmError("неизвестная ошибка обращения к модели")


def _parse_json(text: str) -> dict:
    from .prompts import parse_answer

    try:
        return parse_answer(text)
    except ValueError:
        return {}

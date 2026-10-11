"""Клиент Ollama: один файл — один stateless-запрос, устойчивый к любым сбоям.

Два соображения, из-за которых здесь всё аккуратно:

* Контекст. Модель на 12 ГБ VRAM падает с HTTP 500, если в prompt_eval уходит
  30-40 тыс. токенов. Поэтому запрос всегда одноплатёжный (без истории), а
  budget-блоки считает вызывающий код; сюда приходит только готовый текст.
* Чистота ответа. Ollama кэширует KV по префиксу промпта. Чтобы предыдущий
  файл не «подтёкал» в следующий, в system добавляется уникальный nonce
  (policy=nonce) либо модель полностью выгружается перед запросом
  (policy=unload, медленнее: прогреть 8-17 ГБ заново).

Устойчивость (чтобы пользователь не видел «ошибок Ollama»):

* любой сетевой сбой (обрыв, сброс соединения, обрезанный ответ, таймаут,
  «сервер занят») — это повтор с нарастающей паузой, а не трейсбек;
* запросы к своему/локальному компьютеру идут МИМО системного прокси
  (иначе на Windows с корпоративным прокси 127.0.0.1 отвечает «502»);
* модель, которой нет, находится по имени с тегом ``:latest``; если её правда
  нет — понятное сообщение с командой ``ollama pull``, а не «HTTP 404»;
* сервер не знает ``think`` / ``format``-схему / картинки — клиент убирает
  лишнее и повторяет, а не падает;
* не хватило памяти под num_ctx — окно честно уменьшается, а вызывающий код
  узнаёт об этом и пересобирает промпт под меньшее окно;
* пустой, обрезанный или не-JSON ответ — повтор с другим режимом формата.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_TIMEOUT = 600
DEFAULT_PORT = 11434
MIN_CTX = 4096                  # ниже инструкция регламента не помещается
_BACKOFF_CAP = 20.0


# ----------------------------------------------------------------- исключения
class LlmError(RuntimeError):
    """Обычная ошибка обращения к модели (пишем в журнал, строка идёт в review)."""


class ContextOverflow(LlmError):
    """Контекст не влез: вызывающий код обязан ужать блоки и повторить.

    ``new_ctx`` — окно, на которое клиент уже перешёл (0 — окно не менялось)."""

    def __init__(self, message: str = "", new_ctx: int = 0):
        super().__init__(message)
        self.new_ctx = int(new_ctx or 0)


class LlmTimeout(LlmError):
    """Модель не ответила за timeout_seconds. Повторять такой запрос бесполезно."""


class OllamaUnavailable(LlmError):
    """Ollama не запущена, не отвечает или оборвала соединение."""


class ModelMissing(LlmError):
    """Такой модели в Ollama нет. ``model`` — как её просили, ``hint`` — что делать."""

    def __init__(self, model: str, available: list[str] | None = None):
        self.model = model
        self.available = list(available or [])
        have = (f" Установлены: {', '.join(self.available[:8])}." if self.available
                else " Установленных моделей нет.")
        super().__init__(f"модель «{model}» не найдена в Ollama.{have} "
                         f"Скачайте её: ollama pull {model}")


class VisionUnsupported(LlmError):
    """Модель не принимает картинки (или Ollama без поддержки зрения для неё)."""


# ---------------------------------------------------------------- вспомогалки
def normalize_model(name: str) -> str:
    """``gemma3`` ≡ ``gemma3:latest``; регистр не важен."""
    value = (name or "").strip().lower()
    if not value:
        return ""
    last = value.rsplit("/", 1)[-1]
    return value if ":" in last else value + ":latest"


def same_model(a: str, b: str) -> bool:
    return bool(a) and normalize_model(a) == normalize_model(b)


def estimate_tokens_text(text: str) -> int:
    """Грубая оценка токенов русского текста (согласована с config_loader)."""
    return int(len(text or "") / 2.6)


def _clean_base(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    # OLLAMA_HOST бывает «0.0.0.0» (адрес для прослушивания) — подключаться надо к loopback
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "http"
    if port is None and scheme == "http":
        port = DEFAULT_PORT
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port else "") + parsed.path.rstrip("/")


def _is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def candidate_urls(base_url: str = "") -> list[str]:
    """Где может висеть Ollama: настройка, переменная OLLAMA_HOST, локальные адреса."""
    urls: list[str] = []

    def add(value: str) -> None:
        cleaned = _clean_base(value)
        if cleaned and cleaned not in urls:
            urls.append(cleaned)

    add(base_url)
    first = urls[0] if urls else ""
    if first and not _is_loopback_url(first):
        return urls                       # адрес удалённого ПК с видеокартой задан явно — на другую машину не уходим
    add(os.environ.get("OLLAMA_HOST", ""))
    add("http://127.0.0.1:11434")
    if "http://127.0.0.1:11434" not in urls:        # «localhost» — тот же адрес; лишняя проба на Windows ~2 с
        add("http://localhost:11434")
    return urls


def _is_local_host(host: str) -> bool:
    """Свой компьютер или локальная сеть: прокси здесь только мешает."""
    host = (host or "").strip("[]").lower()
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
    except ValueError:
        return (host == "localhost" or "." not in host
                or host.endswith((".local", ".lan", ".home", ".internal")))


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SYSTEM = urllib.request.build_opener()


def _opener_for(url: str):
    host = urllib.parse.urlsplit(url).hostname or ""
    return _DIRECT if _is_local_host(host) else _SYSTEM


# сетевые сбои, которые не должны превращаться в трейсбек
_NET_ERRORS = (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError,
               http.client.HTTPException, OSError)


def probe_endpoint(base_url: str = "", timeout: int = 5) -> tuple[str, bool]:
    """Найти отвечающий адрес Ollama. Возвращает (url, жив_ли)."""
    urls = candidate_urls(base_url)
    for url in urls:
        try:
            with _opener_for(url).open(f"{url}/api/version", timeout=timeout) as response:
                json.loads(response.read().decode("utf-8"))
            return url, True
        except Exception:
            continue
    return (urls or [f"http://127.0.0.1:{DEFAULT_PORT}"])[0], False


@dataclass
class LlmResult:
    data: dict = field(default_factory=dict)
    raw: str = ""
    prompt_tokens: int = 0
    answer_tokens: int = 0
    seconds: float = 0.0
    model: str = ""
    attempts: int = 0
    notes: list[str] = field(default_factory=list)   # что клиент подправил по дороге


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")[:600]
    except Exception:
        return str(exc.reason or "")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("error"):
            return str(parsed["error"])[:500]
    except ValueError:
        pass
    return raw or str(exc.reason or "")


_MEMORY_HINTS = ("more system memory", "out of memory", "cuda error", "cudamalloc",
                 "failed to allocate", "unable to allocate", "insufficient memory",
                 "memory layout", "not enough memory", "vram", "ggml_backend",
                 "runner process has terminated", "runner terminated", "model requires more")
_CONTEXT_HINTS = ("context length", "context window", "exceeds", "too long", "increase context",
                  "please increase", "maximum context", "input length", "too many tokens",
                  "prompt is too long", "too many images")


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
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = _clean_base(base_url) or f"http://127.0.0.1:{DEFAULT_PORT}"
        self.model = model
        self.num_ctx = max(MIN_CTX, int(num_ctx))
        self.requested_ctx = self.num_ctx
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
        self._sleep = sleep
        # что выяснилось о сервере по дороге; помним, чтобы не наступать повторно
        self._send_think = True
        self._schema_ok = True
        self._info: dict[str, dict] = {}
        self._installed: list[str] | None = None
        self.log: Callable[[str], None] = lambda msg: None

    # ------------------------------------------------------------- introspect
    def _get(self, path: str, timeout: int = 8):
        url = f"{self.base_url}{path}"
        with _opener_for(url).open(url, timeout=timeout) as resp:
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

    def version(self) -> str:
        try:
            return str(self._get("/api/version").get("version", ""))
        except Exception:
            return ""

    def list_models(self) -> list[str]:
        try:
            names = sorted(m["name"] for m in self._get("/api/tags").get("models", []) if m.get("name"))
        except Exception:
            return []
        self._installed = names
        return names

    def list_models_detailed(self) -> list[dict]:
        try:
            return [m for m in self._get("/api/tags").get("models", []) if m.get("name")]
        except Exception:
            return []

    def has_model(self, model: str | None = None) -> bool:
        wanted = model or self.model
        return any(same_model(wanted, name) for name in self.list_models())

    def resolve_model(self, model: str | None = None) -> str:
        """Точное имя установленной модели (``gemma3`` → ``gemma3:latest``) или ''."""
        wanted = model or self.model
        for name in self.list_models():
            if same_model(wanted, name):
                return name
        return ""

    def loaded(self) -> list[dict]:
        try:
            return self._get("/api/ps").get("models", [])
        except Exception:
            return []

    def show(self, model: str | None = None) -> dict:
        """Описание модели из /api/show (кэшируется); {} — нет модели или связи."""
        name = model or self.model
        if name not in self._info:
            try:
                self._info[name] = self._post("/api/show", {"model": name}, timeout=20, retry=False)
            except LlmError:
                self._info[name] = {}
        return self._info[name]

    def capabilities(self, model: str | None = None) -> list[str]:
        """['completion', 'vision', 'thinking', ...]; пусто — старая Ollama или нет модели."""
        info = self.show(model)
        caps = [str(c).lower() for c in (info.get("capabilities") or [])]
        if not caps and info:
            # Ollama < 0.6.4 не сообщает capabilities — угадываем по описанию модели
            keys = [str(k).lower() for k in (info.get("model_info") or {})]
            if info.get("projector_info") or any(".vision." in k or k.startswith("clip.") for k in keys):
                caps = ["completion", "vision"]
        return caps

    def context_limit(self, model: str | None = None) -> int:
        """Максимальный контекст, который держит сама модель (0 — не знаем)."""
        for key, value in (self.show(model).get("model_info") or {}).items():
            if str(key).endswith(".context_length"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
        return 0

    def supports_vision(self, model: str | None = None) -> bool | None:
        """True/False, а если Ollama не говорит — None (пробуем, сбой обработаем)."""
        caps = self.capabilities(model)
        if not caps:
            return None
        return "vision" in caps

    def unload(self, model: str | None = None) -> None:
        """Сброс KV-кэша: keep_alive=0 выгружает модель из памяти."""
        payload = {"model": model or self.model, "keep_alive": 0}
        try:
            self._post("/api/generate", payload, timeout=60, retry=False)
            self.last_unloaded = True
        except LlmError:
            pass

    def pull(self, model: str, progress: Callable[[str, int, int], None] | None = None) -> bool:
        """Скачать модель (``ollama pull``). progress(статус, скачано, всего)."""
        url = f"{self.base_url}/api/pull"
        body = json.dumps({"model": model, "stream": True}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with _opener_for(url).open(request, timeout=max(60, self.timeout)) as resp:
                for line in resp:
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if event.get("error"):
                        raise LlmError(str(event["error"]))
                    if progress:
                        progress(str(event.get("status", "")), int(event.get("completed") or 0),
                                 int(event.get("total") or 0))
        except urllib.error.HTTPError as exc:
            raise LlmError(f"HTTP {exc.code}: {_error_text(exc)}") from exc
        except _NET_ERRORS as exc:
            raise OllamaUnavailable(f"Ollama недоступна: {exc}") from exc
        self._installed = None
        return True

    # ------------------------------------------------------------------ calls
    def _post(self, path: str, payload: dict, timeout: int | None = None, retry: bool = True) -> dict:
        """POST JSON → dict. Все сетевые сбои превращаются в подклассы LlmError.

        Сеть повторяется здесь (обрыв, перегрузка); смысловые ошибки (контекст,
        формат, отсутствие модели) вызывающий код разбирает сам по типу."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
        last: LlmError | None = None
        attempts = (2 if retry else 0) + 1
        for attempt in range(attempts):
            url = f"{self.base_url}{path}"
            request = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
            try:
                with _opener_for(url).open(request, timeout=timeout or self.timeout) as resp:
                    raw = resp.read()
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise LlmError(
                        f"Ollama вернула не-JSON ({type(exc).__name__}): {raw[:120]!r}") from exc
                if not isinstance(parsed, dict):
                    raise LlmError(f"Ollama вернула не объект: {raw[:120]!r}")
                if parsed.get("error") and "message" not in parsed and "response" not in parsed:
                    raise self._classify(500, str(parsed["error"]))   # 200 с телом {"error": …}
                return parsed
            except urllib.error.HTTPError as exc:
                error = self._classify(exc.code, _error_text(exc), exc.headers.get("Retry-After") if exc.headers else None)
                if not getattr(error, "transient", False):
                    raise error from exc
                last = error
            except LlmError:
                raise
            except (socket.timeout, TimeoutError) as exc:
                raise LlmTimeout(f"таймаут {timeout or self.timeout}s — модель не ответила "
                                 "(слабый компьютер или модель ещё грузится; увеличьте "
                                 "timeout_seconds или возьмите модель поменьше)") from exc
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    raise LlmTimeout(f"таймаут {timeout or self.timeout}s — модель не ответила") from exc
                last = OllamaUnavailable(f"Ollama недоступна ({self.base_url}): {reason}")
            except _NET_ERRORS as exc:     # обрыв, сброс соединения, RemoteDisconnected, IncompleteRead
                last = OllamaUnavailable(
                    f"соединение с Ollama оборвано ({type(exc).__name__}: {exc})")
            if attempt < attempts - 1:
                self._sleep(min(_BACKOFF_CAP, 1.5 * (attempt + 1)))
                if isinstance(last, OllamaUnavailable):   # Ollama могла перезапуститься на другом адресе
                    found, alive = probe_endpoint(self.base_url, timeout=3)
                    if alive:
                        self.base_url = found
        raise last or LlmError("неизвестная сетевая ошибка")

    def _classify(self, code: int, detail: str, retry_after: str | None = None) -> LlmError:
        error = self._classify_text(code, detail, retry_after)
        error.http = True            # type: ignore[attr-defined]  # ответ сервера, а не наш разбор
        return error

    def _classify_text(self, code: int, detail: str, retry_after: str | None = None) -> LlmError:
        low = (detail or "").lower()
        text = f"HTTP {code}: {detail or 'ошибка Ollama'}"
        if "model" in low and ("not found" in low or "try pulling" in low) and code in (400, 404, 500):
            return ModelMissing(self.model, self._installed or self.list_models())
        if code == 400 and any(w in low for w in ("image", "vision", "multimodal", "mmproj", "projector")):
            return VisionUnsupported(text)
        if any(h in low for h in _CONTEXT_HINTS):
            return ContextOverflow(text)
        if any(h in low for h in _MEMORY_HINTS):
            err = ContextOverflow(text)
            err.memory = True          # type: ignore[attr-defined]
            return err
        if code in (429, 502, 503, 504):
            err = LlmError(f"Ollama занята или перезапускается ({text})")
            err.transient = True       # type: ignore[attr-defined]
            if retry_after and str(retry_after).isdigit():
                self._sleep(min(_BACKOFF_CAP, float(retry_after)))
            return err
        return LlmError(text)

    # ----------------------------------------------------------------- запрос
    def _payload(self, system: str, user: str, schema: dict | None, images: list[str] | None,
                 num_predict: int, force_plain_json: bool) -> dict:
        prompt_system = system
        if self.cache_policy == "nonce":
            prompt_system = f"{system}\n\n[запрос {uuid.uuid4().hex[:12]} — отвечай только по этому файлу]"
        enforced = bool(self.structured and schema and self._schema_ok and not force_plain_json)
        if schema and not enforced:
            user = user + _schema_hint(schema)      # без грамматики ключи надо назвать словами
        messages = [{"role": "system", "content": prompt_system}, {"role": "user", "content": user}]
        if images:
            messages[-1]["images"] = images
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "num_ctx": self.num_ctx,
                "num_predict": num_predict,
            },
        }
        payload["format"] = schema if enforced else "json"
        if self._send_think:
            payload["think"] = bool(self.enable_thinking)      # False — «не размышляй»; True — если включено в настройках
        return payload

    def _shrink_ctx(self, system: str, user: str, images: list[str] | None) -> int:
        """Уменьшить окно после нехватки памяти. Возвращает новое окно или 0, если уже некуда."""
        smaller = max(MIN_CTX, int(self.num_ctx * 0.6) // 1024 * 1024)
        if smaller >= self.num_ctx:
            return 0
        self.log(f"  не хватило памяти под окно {self.num_ctx} ток. → пробую {smaller}")
        self.num_ctx = smaller
        return smaller

    def ask(self, system: str, user: str, schema: dict | None = None, images: list[str] | None = None) -> LlmResult:
        """Один изолированный запрос. История не ведётся намеренно."""
        notes: list[str] = []
        num_predict = self.num_predict
        force_plain = False
        last_error: Exception | None = None
        attempt = 0
        max_attempts = self.retries + 1
        extra_budget = 3                    # «ремонтные» повторы не съедают обычные попытки
        used_images = images

        while attempt < max_attempts:
            attempt += 1
            if self.cache_policy == "unload":
                self.unload()
            payload = self._payload(system, user, schema, used_images, num_predict, force_plain)
            started = time.monotonic()
            try:
                response = self._post("/api/chat", payload)
                message = response.get("message")
                if not isinstance(message, dict):
                    raise LlmError(f"ответ не похож на ответ Ollama: {str(response)[:120]}")
                raw = message.get("content", "") or ""
                if not raw.strip() and message.get("thinking"):
                    # модель «думала» вслух и не дошла до ответа: просим без размышлений
                    notes.append("пустой ответ после размышлений")
                    raise LlmError("модель потратила ответ на размышления")
                result = LlmResult(
                    raw=raw,
                    prompt_tokens=int(response.get("prompt_eval_count") or 0),
                    answer_tokens=int(response.get("eval_count") or 0),
                    seconds=round(time.monotonic() - started, 1),
                    model=self.model,
                    attempts=attempt,
                    notes=notes,
                )
                result.data = _parse_json(raw)
                if not result.data:
                    cut = str(response.get("done_reason", "")) == "length"
                    if cut and num_predict < 4096:
                        num_predict = min(4096, int(num_predict * 1.6))
                        notes.append(f"ответ обрезан — лимит поднят до {num_predict}")
                        if extra_budget > 0:
                            extra_budget -= 1
                            max_attempts += 1
                    raise LlmError(f"не разобран ответ: {raw[:160] or '(пусто)'}")
                return result
            except ContextOverflow as exc:
                last_error = exc
                if getattr(exc, "memory", False) and (new_ctx := self._shrink_ctx(system, user, used_images)):
                    exc.new_ctx = new_ctx
                    # промпт, рассчитанный на большее окно, мог не влезть — решает вызывающий код
                    if estimate_tokens_text(system) + estimate_tokens_text(user) + num_predict > new_ctx * 0.92:
                        raise
                    notes.append(f"окно уменьшено до {new_ctx}")
                    if extra_budget > 0:
                        extra_budget -= 1
                        max_attempts += 1
                    continue
                break    # ужатием контекста занимается вызывающий код
            except VisionUnsupported as exc:
                if used_images:
                    raise
                last_error = exc
                break
            except (ModelMissing, LlmTimeout):
                raise
            except OllamaUnavailable as exc:
                last_error = exc
                if attempt < max_attempts:
                    self._sleep(min(_BACKOFF_CAP, 2.0 * attempt))
            except LlmError as exc:
                last_error = exc
                low = str(exc).lower()
                from_server = getattr(exc, "http", False)
                if from_server and "thinking" in low and self._send_think and "does not support" in low:
                    self._send_think = False
                    notes.append("модель без режима размышлений — параметр think отключён")
                    if extra_budget > 0:
                        extra_budget -= 1
                        max_attempts += 1
                    continue
                if from_server and self.structured and schema and self._schema_ok and any(
                        w in low for w in ("grammar", "schema", "format", "json schema", "unmarshal")):
                    self._schema_ok = False
                    notes.append("схема ответа не принята — перехожу на простой JSON")
                    if extra_budget > 0:
                        extra_budget -= 1
                        max_attempts += 1
                    continue
                if "не разобран ответ" in low or "размышлени" in low:
                    force_plain = True      # следующий проход — мягкий формат json
                if attempt < max_attempts:
                    self._sleep(min(_BACKOFF_CAP, 2.0 * attempt))
        raise last_error if last_error else LlmError("неизвестная ошибка обращения к модели")


def _schema_hint(schema: dict | None) -> str:
    """Словесное описание схемы — для режима «простой JSON», где грамматику никто не навязывает."""
    props = (schema or {}).get("properties") or {}
    if not props:
        return ""
    lines = []
    for key, spec in props.items():
        kind = spec.get("type", "string")
        allowed = spec.get("enum") or []
        shown = (" | ".join(json.dumps(v, ensure_ascii=False) for v in allowed)
                 if allowed and len(allowed) <= 8 else kind)
        desc = str(spec.get("description", "")).strip()
        lines.append(f'  "{key}": {shown}' + (f"  // {desc}" if desc else ""))
    return ("\n\nВерни ТОЛЬКО один JSON-объект без пояснений, с этими ключами "
            "(все обязательны, неизвестное — пустая строка):\n{\n" + "\n".join(lines) + "\n}")


def _parse_json(text: str) -> dict:
    from .prompts import parse_answer

    try:
        return parse_answer(text)
    except ValueError:
        return {}


# ------------------------------------------------------------ фабрика клиентов
def client_from_config(config: dict, role: str = "text", **overrides) -> OllamaClient:
    """Единая сборка клиента из config.json — все модули берут настройки отсюда.

    role: ``text`` | ``vision``. Любое поле можно переопределить именованным
    аргументом (num_predict, temperature, retries, cache_policy, num_ctx, model…)."""
    llm = config.get("llm", {}) if isinstance(config, dict) else {}

    def num(key: str, default, cast=int):
        try:
            return cast(llm.get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    model = llm.get("model_text", "gemma3:12b")
    num_ctx = num("num_ctx", 16384)
    if role == "vision":
        model = llm.get("model_vision", model) or model
        num_ctx = min(num_ctx, num("vision_num_ctx", 16384))
    params = dict(
        base_url=llm.get("base_url", f"http://127.0.0.1:{DEFAULT_PORT}"),
        model=model,
        num_ctx=num_ctx,
        num_predict=num("num_predict", 800),
        temperature=num("temperature", 0.1, float),
        top_p=num("top_p", 0.9, float),
        keep_alive=str(llm.get("keep_alive", "30m")),
        timeout=num("timeout_seconds", 900),
        cache_policy=llm.get("cache_policy", "nonce"),
        structured=bool(llm.get("structured_output", True)),
        retries=num("retries", 2),
        enable_thinking=bool(llm.get("enable_thinking", False)),
    )
    params.update(overrides)
    return OllamaClient(**params)

"""Поддельный сервер Ollama для тестов: те же ручки, что у настоящей, плюс сценарные сбои.

Сценарий — очередь действий для ``/api/chat``: каждый запрос берёт следующее
действие, а когда очередь пуста — отвечает ``responder(payload)``.

    server.script(
        {"status": 500, "error": "llama runner process has terminated"},
        {"drop": True},                      # оборвать соединение без ответа
        {"content": ""},                     # пустой ответ
        {"content": '{"new_name": "x"'},     # обрезанный JSON
        {"sleep": 0.5},                      # задержка, затем обычный ответ
    )
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def default_responder(payload: dict) -> dict:
    schema = payload.get("format")
    if isinstance(schema, dict) and schema.get("properties"):
        out = {}
        for key, spec in schema["properties"].items():
            kind = spec.get("type")
            if spec.get("enum"):
                out[key] = spec["enum"][0]
            elif kind == "number":
                out[key] = 0.95
            elif kind == "boolean":
                out[key] = False
            else:
                out[key] = ""
        return out
    return {"ok": True}


class FakeOllama:
    def __init__(self, models: dict[str, list[str]] | None = None, responder=None, version: str = "0.12.0"):
        # имя → capabilities
        self.models = models if models is not None else {"gemma3:12b": ["completion", "vision"]}
        self.responder = responder or default_responder
        self.version = version
        self.requests: list[tuple[str, dict]] = []
        self.chat_payloads: list[dict] = []
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self.reject_think = False          # имитация «does not support thinking»
        self.reject_schema = False         # имитация сбоя грамматики
        self.pulled: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):    # тишина в выводе тестов
                pass

            def _send(self, code: int, body: dict | bytes, headers: dict | None = None):
                raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/version":
                    self._send(200, {"version": outer.version})
                elif self.path == "/api/tags":
                    self._send(200, {"models": [{"name": n, "size": 8_000_000_000} for n in outer.models]})
                elif self.path == "/api/ps":
                    self._send(200, {"models": []})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                with outer._lock:
                    outer.requests.append((self.path, payload))
                if self.path == "/api/show":
                    name = payload.get("model", "")
                    if name not in outer.models:
                        return self._send(404, {"error": f'model "{name}" not found'})
                    return self._send(200, {"capabilities": outer.models[name], "details": {}})
                if self.path == "/api/generate":
                    return self._send(200, {"response": "", "done": True})
                if self.path == "/api/embed":
                    texts = payload.get("input") or []
                    return self._send(200, {"embeddings": [[float(len(t) % 7), 1.0, 0.5] for t in texts]})
                if self.path == "/api/pull":
                    outer.pulled.append(payload.get("model", ""))
                    outer.models.setdefault(payload.get("model", ""), ["completion"])
                    lines = [{"status": "pulling manifest"},
                             {"status": "downloading", "completed": 50, "total": 100},
                             {"status": "success"}]
                    raw = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path == "/api/chat":
                    return self._chat(payload)
                self._send(404, {"error": "unknown route"})

            def _chat(self, payload: dict):
                with outer._lock:
                    outer.chat_payloads.append(payload)
                    action = outer._queue.pop(0) if outer._queue else {}
                if action.get("sleep"):
                    time.sleep(float(action["sleep"]))
                if action.get("drop"):
                    self.close_connection = True
                    self.connection.close()
                    return
                if payload.get("model") not in outer.models:
                    return self._send(404, {"error": f'model "{payload.get("model")}" not found, try pulling it first'})
                if outer.reject_think and "think" in payload:
                    return self._send(400, {"error": f'"{payload["model"]}" does not support thinking'})
                if outer.reject_schema and isinstance(payload.get("format"), dict):
                    return self._send(500, {"error": "failed to load model vocabulary required for format (grammar)"})
                if payload.get("messages", [{}])[-1].get("images") and \
                        "vision" not in outer.models.get(payload["model"], []):
                    return self._send(400, {"error": "this model is missing data required for image input"})
                if action.get("status"):
                    return self._send(int(action["status"]), {"error": action.get("error", "boom")},
                                      {"Retry-After": "0"} if action["status"] in (429, 503) else None)
                if "content" in action:
                    content = action["content"]
                else:
                    content = json.dumps(outer.responder(payload), ensure_ascii=False)
                body = {"model": payload.get("model"), "message": {"role": "assistant", "content": content},
                        "done": True, "done_reason": action.get("done_reason", "stop"),
                        "prompt_eval_count": action.get("prompt_tokens", 900), "eval_count": 120}
                if action.get("thinking"):
                    body["message"]["thinking"] = "..."
                self._send(200, body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def script(self, *actions: dict) -> None:
        with self._lock:
            self._queue.extend(actions)

    def start(self) -> "FakeOllama":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "FakeOllama":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

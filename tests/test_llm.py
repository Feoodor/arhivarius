"""Клиент Ollama против поддельного сервера: каждый сбой — повтор или понятная ошибка."""
import json
import unittest

from core import llm
from core.llm import (ContextOverflow, LlmError, LlmTimeout, ModelMissing, OllamaClient,
                      OllamaUnavailable, VisionUnsupported, client_from_config)
from core.prompts import RESPONSE_SCHEMA
from tests.fake_ollama import FakeOllama


def make(server: FakeOllama, **kw) -> OllamaClient:
    params = dict(base_url=server.url, model="gemma3:12b", num_ctx=8192, retries=2,
                  cache_policy="none", sleep=lambda s: None)
    params.update(kw)
    return OllamaClient(**params)


GOOD = json.dumps({"new_name": "2026-01-01_Письмо_Тест.docx", "target_path": "Документы/Входящие/2026"},
                  ensure_ascii=False)


class Helpers(unittest.TestCase):
    def test_model_names(self):
        self.assertTrue(llm.same_model("gemma3", "gemma3:latest"))
        self.assertTrue(llm.same_model("Gemma3:12B", "gemma3:12b"))
        self.assertFalse(llm.same_model("gemma3:12b", "gemma3:27b"))
        self.assertEqual(llm.normalize_model("hf.co/user/m"), "hf.co/user/m:latest")

    def test_urls(self):
        self.assertEqual(llm.candidate_urls("0.0.0.0")[0], "http://127.0.0.1:11434")
        self.assertEqual(llm.candidate_urls("pc-gpu")[0], "http://pc-gpu:11434")
        self.assertEqual(llm.candidate_urls("http://192.168.1.5:8080/")[0], "http://192.168.1.5:8080")
        self.assertNotIn("", llm.candidate_urls(""))
        self.assertEqual(llm.candidate_urls("http://host:99999x")[0], "http://127.0.0.1:11434")

    def test_proxy_only_for_remote_hosts(self):
        self.assertTrue(llm._is_local_host("127.0.0.1"))
        self.assertTrue(llm._is_local_host("192.168.0.20"))
        self.assertTrue(llm._is_local_host("gpu-box"))
        self.assertFalse(llm._is_local_host("ollama.example.com"))


class Introspection(unittest.TestCase):
    def test_alive_models_caps(self):
        with FakeOllama({"gemma3:12b": ["completion", "vision"], "qwen3:14b": ["completion"]}) as s:
            c = make(s)
            self.assertTrue(c.is_alive())
            self.assertEqual(c.list_models(), ["gemma3:12b", "qwen3:14b"])
            self.assertTrue(c.has_model("gemma3:12b"))
            self.assertFalse(c.has_model("llama3"))
            self.assertTrue(c.supports_vision("gemma3:12b"))
            self.assertFalse(c.supports_vision("qwen3:14b"))
            self.assertIsNone(c.supports_vision("absent"))      # неизвестно — не «нет»

    def test_latest_tag_resolves(self):
        with FakeOllama({"gemma3:latest": ["completion"]}) as s:
            self.assertEqual(make(s).resolve_model("gemma3"), "gemma3:latest")

    def test_dead_server_is_not_alive_and_not_exception(self):
        c = OllamaClient(base_url="http://127.0.0.1:1", retries=0, sleep=lambda s: None)
        self.assertFalse(c.is_alive())
        self.assertEqual(c.list_models(), [])
        self.assertEqual(c.loaded(), [])


class Ask(unittest.TestCase):
    def test_success_structured(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            r = make(s).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertEqual(r.data["new_name"], "2026-01-01_Письмо_Тест.docx")
            self.assertEqual(r.prompt_tokens, 900)
            sent = s.chat_payloads[-1]
            self.assertIsInstance(sent["format"], dict)
            self.assertIs(sent["think"], False)
            self.assertFalse(sent["stream"])

    def test_connection_drop_is_retried(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"drop": True}, {"drop": True})
            r = make(s).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertEqual(r.data["target_path"], "Документы/Входящие/2026")

    def test_server_error_then_success(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"status": 500, "error": "something odd"})
            r = make(s).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertEqual(r.attempts, 2)

    def test_busy_503_is_retried(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"status": 503, "error": "server busy"})
            self.assertTrue(make(s).ask("sys", "user", RESPONSE_SCHEMA).data)

    def test_empty_then_truncated_then_ok(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"content": ""}, {"content": '{"new_name": "a.docx", "target_path": "x'})
            r = make(s, retries=3).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertTrue(r.data.get("new_name"))

    def test_plain_json_fallback_carries_key_hint(self):
        """После неразобранного ответа формат падает до «json», но ключи называются словами."""
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"content": "прости, не могу"})
            make(s).ask("sys", "user", RESPONSE_SCHEMA)
            second = s.chat_payloads[1]
            self.assertEqual(second["format"], "json")
            self.assertIn("new_name", second["messages"][-1]["content"])

    def test_length_cut_raises_limit(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"content": "", "done_reason": "length"})
            make(s, num_predict=200).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertGreater(s.chat_payloads[1]["options"]["num_predict"], 200)

    def test_thinking_unsupported_is_dropped(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.reject_think = True
            r = make(s).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertTrue(r.data)
            self.assertNotIn("think", s.chat_payloads[-1])
            self.assertTrue(any("think" in n for n in r.notes))

    def test_grammar_failure_falls_back_to_plain_json(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.reject_schema = True
            r = make(s).ask("sys", "user", RESPONSE_SCHEMA)
            self.assertTrue(r.data)
            self.assertEqual(s.chat_payloads[-1]["format"], "json")

    def test_missing_model_is_actionable(self):
        with FakeOllama({"qwen3:14b": ["completion"]}) as s:
            with self.assertRaises(ModelMissing) as cm:
                make(s, model="gemma3:12b").ask("sys", "user", RESPONSE_SCHEMA)
            self.assertIn("ollama pull gemma3:12b", str(cm.exception))
            self.assertIn("qwen3:14b", str(cm.exception))

    def test_images_on_text_model(self):
        with FakeOllama({"qwen3:14b": ["completion"]}) as s:
            with self.assertRaises(VisionUnsupported):
                make(s, model="qwen3:14b").ask("sys", "user", None, images=["AAAA"])

    def test_dead_server_gives_unavailable(self):
        c = OllamaClient(base_url="http://127.0.0.1:1", retries=1, sleep=lambda s: None)
        with self.assertRaises(OllamaUnavailable):
            c.ask("sys", "user")

    def test_timeout_is_not_retried(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"sleep": 1.5})
            c = make(s, timeout=1)
            with self.assertRaises(LlmTimeout):
                c.ask("sys", "user", RESPONSE_SCHEMA)
            self.assertEqual(len(s.chat_payloads), 1)

    def test_no_exception_leaks_other_than_llmerror(self):
        """Что бы ни случилось, наружу выходит LlmError (его ловит конвейер)."""
        with FakeOllama() as s:
            s.script(*[{"drop": True}] * 12)
            with self.assertRaises(LlmError):
                make(s).ask("sys", "user", RESPONSE_SCHEMA)


class MemoryPressure(unittest.TestCase):
    def test_oom_shrinks_window_and_retries_small_prompt(self):
        with FakeOllama(responder=lambda p: json.loads(GOOD)) as s:
            s.script({"status": 500, "error": "llama runner process has terminated: cudaMalloc failed: out of memory"})
            c = make(s, num_ctx=32768)
            r = c.ask("коротко", "коротко", RESPONSE_SCHEMA)
            self.assertTrue(r.data)
            self.assertLess(c.num_ctx, 32768)
            self.assertEqual(s.chat_payloads[-1]["options"]["num_ctx"], c.num_ctx)

    def test_oom_with_big_prompt_tells_caller_new_window(self):
        with FakeOllama() as s:
            s.script({"status": 500, "error": "out of memory"})
            c = make(s, num_ctx=32768)
            with self.assertRaises(ContextOverflow) as cm:
                c.ask("я" * 60000, "я" * 20000, RESPONSE_SCHEMA)
            self.assertEqual(cm.exception.new_ctx, c.num_ctx)
            self.assertLess(c.num_ctx, 32768)

    def test_context_error_goes_to_caller(self):
        with FakeOllama() as s:
            s.script({"status": 400, "error": "input length exceeds the context length"})
            with self.assertRaises(ContextOverflow):
                make(s).ask("sys", "user", RESPONSE_SCHEMA)


class Pull(unittest.TestCase):
    def test_pull_reports_progress(self):
        with FakeOllama({"gemma3:12b": ["completion"]}) as s:
            seen = []
            self.assertTrue(make(s).pull("qwen3:8b", lambda st, done, total: seen.append((st, done, total))))
            self.assertIn(("downloading", 50, 100), seen)
            self.assertIn("qwen3:8b", s.pulled)


class Factory(unittest.TestCase):
    def test_roles(self):
        cfg = {"llm": {"model_text": "a", "model_vision": "b", "num_ctx": 32768, "vision_num_ctx": 8192,
                       "base_url": "http://h:1", "num_predict": 300}}
        text = client_from_config(cfg)
        vision = client_from_config(cfg, "vision")
        self.assertEqual((text.model, text.num_ctx), ("a", 32768))
        self.assertEqual((vision.model, vision.num_ctx), ("b", 8192))
        self.assertEqual(client_from_config(cfg, num_predict=50).num_predict, 50)

    def test_garbage_config_does_not_crash(self):
        c = client_from_config({"llm": {"num_ctx": "много", "temperature": None}})
        self.assertGreaterEqual(c.num_ctx, 4096)


if __name__ == "__main__":
    unittest.main()

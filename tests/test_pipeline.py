"""Конвейер разбора целиком: сканер → индекс → Analyzer → поддельная Ollama.

Главное, что проверяем: что бы ни случилось с Ollama, партия не падает трейсбеком,
готовые решения сохраняются, а неудачные файлы возвращаются в очередь."""
import json
import tempfile
import unittest
from pathlib import Path

from core import mapio
from core.llm import ModelMissing, OllamaUnavailable
from core.pipeline import Analyzer, Filters
from tests.fake_ollama import FakeOllama
from tests.helpers import department_responder, make_archive, scan_and_index


def build(tmp, server, files=None, **llm):
    config, root = make_archive(Path(tmp), files, {"base_url": server.url, **llm})
    snapshot, index = scan_and_index(config)
    logs: list[str] = []
    analyzer = Analyzer(config, snapshot, index, log=logs.append)
    return analyzer, config, logs


class HappyPath(unittest.TestCase):
    def test_batch_gives_sensible_names_and_places(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            analyzer, config, logs = build(tmp, server)
            records = analyzer.run(Filters(limit=10))
            by_name = {r["Текущее имя"]: r for r in records}
            self.assertEqual(len(records), 3)
            letter = by_name["scan001.txt"]
            self.assertEqual(letter["Целевой путь"], "Документы/Входящие/2026")
            self.assertTrue(letter["Новое имя"].startswith("2026-03-12_Вх-123_Письмо"))
            self.assertEqual(letter["Статус"], "готово")
            petition = by_name["doc2.txt"]
            self.assertEqual(petition["Целевой путь"], "Документы/Прошения - Рапорты/2026")
            participants = by_name["notes.txt"]
            self.assertEqual(participants["Статус"], "проверка")       # персональные данные — на глаз
            self.assertIn("06_Участники", participants["Целевой путь"])
            self.assertEqual(analyzer.stopped_reason, "")
            saved = json.loads((config["output_path"] / "suggestions.json").read_text("utf-8"))
            self.assertEqual(len(saved), 3)

    def test_second_batch_skips_done_files(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            analyzer, config, _ = build(tmp, server)
            analyzer.run(Filters(limit=10))
            self.assertEqual(analyzer.queue(Filters(limit=10)), [])


class Failures(unittest.TestCase):
    def test_failed_file_is_requeued_others_survive(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            analyzer, config, logs = build(tmp, server, retries=0)
            analyzer.client._sleep = lambda s: None
            # первый файл (по порядку) получает три обрыва подряд, остальные — норма
            server.script(*[{"drop": True}] * 3)
            records = analyzer.run(Filters(limit=10))
            self.assertEqual(len(records), 3)
            failed = [r for r in records if r["_ошибка"]]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["Статус"], "проверка")
            self.assertIn("сбой модели", failed[0]["Причина"])
            self.assertIs(failed[0]["_окончено"], False)
            # в карту строка попала, но следующая партия возьмёт файл снова
            rows = mapio.load_map(config["output_path"] / "migration_map.csv")
            rows, *_ = mapio.merge_suggestions(rows, records)
            mapio.save_map(config["output_path"] / "migration_map.csv", rows)
            again = analyzer.queue(Filters(limit=10))
            self.assertEqual([i["name"] for i in again], [failed[0]["Текущее имя"]])

    def test_ollama_dies_midway_batch_stops_cleanly(self):
        many = {f"letter{i}.txt": f"Входящее письмо Вх-123 от 12.03.2026. Приложение {i}." for i in range(5)}
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeOllama(responder=department_responder).start()
            analyzer, config, logs = build(tmp, server, files=many, retries=0)
            analyzer.client._sleep = lambda s: None
            calls = {"n": 0}
            original = server.responder

            def dying(payload):
                calls["n"] += 1
                if calls["n"] >= 2:
                    server.stop()            # после второго файла сервер исчезает
                return original(payload)

            server.responder = dying
            records = analyzer.run(Filters(limit=10))
            self.assertGreaterEqual(len(records), 2)
            self.assertLess(len(records), 5)         # остальные файлы остались в очереди
            self.assertTrue(analyzer.stopped_reason)
            self.assertTrue(any("ОСТАНОВЛЕНО" in m for m in logs))
            self.assertEqual([r["_окончено"] for r in records if r["_ошибка"]],
                             [False] * len([r for r in records if r["_ошибка"]]))

    def test_ollama_not_running_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeOllama().start()
            analyzer, config, _ = build(tmp, server)
            server.stop()
            with self.assertRaises(OllamaUnavailable) as cm:
                analyzer.run(Filters(limit=10))
            self.assertIn("Запустите", str(cm.exception))

    def test_memory_error_shrinks_window_and_still_answers(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            analyzer, config, logs = build(tmp, server, files={"scan001.txt": "Входящее письмо Вх-123 от 12.03.2026."},
                                           num_ctx=32768)
            server.script({"status": 500, "error": "llama runner process has terminated: out of memory"})
            records = analyzer.run(Filters(limit=5))
            self.assertEqual(len(records), 1)
            self.assertFalse(records[0]["_ошибка"], records[0]["_ошибка"])
            self.assertLess(analyzer.client.num_ctx, 32768)
            self.assertEqual(records[0]["Целевой путь"], "Документы/Входящие/2026")


class Models(unittest.TestCase):
    def test_missing_model_is_replaced_by_installed_one(self):
        models = {"qwen3:14b": ["completion"], "gemma3:4b": ["completion", "vision"]}
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(models, department_responder) as server:
            analyzer, config, logs = build(tmp, server)          # в настройках gemma3:12b, её нет
            records = analyzer.run(Filters(limit=10))
            self.assertEqual(len(records), 3)
            self.assertEqual(analyzer.client.model, "gemma3:4b")  # семейство gemma3 в приоритете
            self.assertTrue(any("не установлена" in m for m in logs))
            self.assertEqual(config["llm"]["model_text"], "gemma3:12b")   # настройки не переписаны

    def test_no_models_at_all(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama({}) as server:
            analyzer, config, _ = build(tmp, server)
            with self.assertRaises((ModelMissing, Exception)) as cm:
                analyzer.run(Filters(limit=10))
            self.assertIn("ollama pull", str(cm.exception))

    def test_latest_tag_matches(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama({"gemma3:latest": ["completion", "vision"]},
                                                              department_responder) as server:
            analyzer, config, logs = build(tmp, server, model_text="gemma3", model_vision="gemma3")
            analyzer.run(Filters(limit=10))
            self.assertEqual(analyzer.client.model, "gemma3:latest")
            self.assertFalse(any("не установлена" in m for m in logs))

    def test_vision_falls_back_when_model_cannot_see(self):
        models = {"gemma3:12b": ["completion"]}                  # без vision
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(models, department_responder) as server:
            analyzer, config, logs = build(tmp, server, vision=True)
            analyzer.run(Filters(limit=10))
            self.assertFalse(analyzer.vision_enabled)
            self.assertTrue(any("зрение" in m.lower() for m in logs))

    def test_num_ctx_clamped_to_model_limit(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOllama(responder=department_responder) as server:
            analyzer, config, logs = build(tmp, server, num_ctx=32768)
            original_show = analyzer.client.show
            analyzer.client.show = lambda model=None: {**original_show(model),
                                                       "model_info": {"gemma3.context_length": 8192}}
            analyzer.run(Filters(limit=10))
            self.assertEqual(analyzer.client.num_ctx, 8192)


if __name__ == "__main__":
    unittest.main()

"""compare_models.py: одни и те же файлы разбирают разные модели, результаты рядом."""
import tempfile
import unittest
from pathlib import Path

import compare_models as cm
from core import pipeline
from tests.fake_ollama import FakeOllama
from tests.helpers import department_responder, make_archive, scan_and_index


class SampleQueue(unittest.TestCase):
    def test_even_spread_not_just_first_files(self):
        items = [{"path": f"a/{i}.txt"} for i in range(100)]
        picked = cm.sample_queue(items, 5)
        self.assertEqual([p["path"] for p in picked], ["a/0.txt", "a/20.txt", "a/40.txt", "a/60.txt", "a/80.txt"])
        self.assertEqual(len(cm.sample_queue(items[:3], 5)), 3)


class Compare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeOllama({"gemma3:4b": ["completion", "vision"], "qwen3.5:9b": ["completion", "vision"]},
                                 responder=department_responder).start()
        self.addCleanup(self.server.stop)
        self.config, _ = make_archive(Path(self.tmp.name), None, {"base_url": self.server.url})
        self.snapshot, self.index = scan_and_index(self.config)
        probe = pipeline.Analyzer(self.config, self.snapshot, self.index)
        self.files = probe.queue(pipeline.Filters(limit=100, skip_done=False))

    def test_two_models_side_by_side(self):
        self.assertEqual(len(self.files), 3)
        results = [cm.run_model(self.config, self.snapshot, self.index, m, self.files, log=lambda s: None)
                   for m in ("gemma3:4b", "qwen3.5:9b")]
        for res in results:
            self.assertEqual(len(res["rows"]), 3)
            self.assertEqual(res["error"], "")
        text = "\n".join(cm.build_report(self.files, results))
        self.assertIn("gemma3:4b", text)
        self.assertIn("qwen3.5:9b", text)
        self.assertIn("Документы/Входящие/2026", text)
        self.assertIn("СОВПАДЕНИЕ ПАПОК", text)
        self.assertIn("3/3", text)                      # обе модели положили всё в одни папки

    def test_compare_does_not_touch_the_map(self):
        cm.run_model(self.config, self.snapshot, self.index, "gemma3:4b", self.files, log=lambda s: None)
        out = self.config["output_path"]
        self.assertFalse((out / "suggestions.json").exists())

    def test_missing_model_is_reported_not_crashed(self):
        res = cm.run_model(self.config, self.snapshot, self.index, "nope:1b", self.files, log=lambda s: None)
        # preflight подставляет лучшую установленную вместо отсутствующей и сообщает об этом в итоге
        text = "\n".join(cm.build_report(self.files, [res]))
        self.assertIn("nope:1b", text)
        self.assertTrue(res["used"] != "nope:1b" or res["error"])

    def test_ollama_down_gives_message(self):
        self.server.stop()
        res = cm.run_model(self.config, self.snapshot, self.index, "gemma3:4b", self.files, log=lambda s: None)
        self.assertEqual(res["rows"], {})
        self.assertIn("Ollama", res["error"])
        self.assertIn("не запустилась", "\n".join(cm.build_report(self.files, [res])))


if __name__ == "__main__":
    unittest.main()

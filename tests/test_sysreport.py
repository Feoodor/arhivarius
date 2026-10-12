"""sysreport.py: разбор данных о железе и отчёт против поддельной Ollama."""
import struct
import unittest
import zlib

import sysreport
from tests.fake_ollama import FakeOllama

NVIDIA = "NVIDIA GeForce RTX 3060, 12288, 1024, 551.23\nNVIDIA GeForce GT 710, 2048, 0, 551.23\n"
MEMINFO = "MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    8192000 kB\n"


class Parsing(unittest.TestCase):
    def test_nvidia_smi(self):
        gpus = sysreport.parse_nvidia_smi(NVIDIA)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["vram"], 12288 * 1024 ** 2)
        self.assertEqual(gpus[0]["driver"], "551.23")
        self.assertEqual(sysreport.parse_nvidia_smi("No devices were found"), [])

    def test_meminfo(self):
        total, free = sysreport.parse_meminfo(MEMINFO)
        self.assertEqual(total, 16384000 * 1024)
        self.assertEqual(free, 8192000 * 1024)

    def test_budget_prefers_vram_then_ram(self):
        gpu = [{"name": "RTX", "vram": 12 * sysreport.GB, "vendor": "NVIDIA"}]
        budget, source = sysreport.model_budget(gpu, 32 * sysreport.GB)
        self.assertEqual(budget, 12 * sysreport.GB)
        self.assertIn("видеопамять", source)
        weak = [{"name": "Intel UHD", "vram": 1 * sysreport.GB, "vendor": "Intel"}]
        budget, source = sysreport.model_budget(weak, 16 * sysreport.GB)
        self.assertAlmostEqual(budget / sysreport.GB, 16 * 0.55, places=1)
        self.assertIn("оперативная", source)
        apple = [{"name": "Apple M2", "vram": 16 * sysreport.GB, "vendor": "Apple", "unified": True}]
        self.assertAlmostEqual(sysreport.model_budget(apple, 16 * sysreport.GB)[0] / sysreport.GB, 10.4, places=1)

    def test_tiers_grow_with_budget(self):
        order = ["ничего крупнее 1-2B", "до 4B", "до 8B", "до 14B", "до 27B", "до 32B+"]
        got = [order.index(sysreport.size_tiers(g * sysreport.GB)) for g in (3, 6, 8, 12, 24, 48)]
        self.assertEqual(got, sorted(got))
        self.assertEqual(sysreport.size_tiers(12 * sysreport.GB), "до 14B")

    def test_clean_base(self):
        self.assertEqual(sysreport.clean_base(""), "http://127.0.0.1:11434")
        self.assertEqual(sysreport.clean_base("192.168.1.5"), "http://192.168.1.5:11434")
        self.assertEqual(sysreport.clean_base("http://host:9/"), "http://host:9")

    def test_png_is_valid(self):
        png = sysreport.make_png(8, 8)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        pos, chunks = 8, []
        while pos < len(png):
            size, kind = struct.unpack(">I4s", png[pos:pos + 8])
            body = png[pos + 8:pos + 8 + size]
            crc = struct.unpack(">I", png[pos + 8 + size:pos + 12 + size])[0]
            self.assertEqual(crc, zlib.crc32(kind + body) & 0xFFFFFFFF)
            chunks.append(kind)
            pos += 12 + size
        self.assertEqual(chunks, [b"IHDR", b"IDAT", b"IEND"])
        idat = png.index(b"IDAT")
        size = struct.unpack(">I", png[idat - 4:idat])[0]
        self.assertEqual(len(zlib.decompress(png[idat + 4:idat + 4 + size])), 8 * (1 + 8 * 3))


class Report(unittest.TestCase):
    def test_unreachable_ollama_is_explained_not_crashed(self):
        lines = sysreport.build_report("http://127.0.0.1:1", False, 3)
        text = "\n".join(lines)
        self.assertIn("НЕ ОТВЕЧАЕТ", text)
        self.assertIn("Процессор", text)

    def test_report_lists_models_and_capabilities(self):
        server = FakeOllama({"gemma3:12b": ["completion", "vision"], "bge-m3": ["embedding"]}).start()
        self.addCleanup(server.stop)
        text = "\n".join(sysreport.build_report(server.url, False, 3))
        self.assertIn("версия", text)
        self.assertIn("gemma3:12b", text)
        self.assertIn("vision", text)
        self.assertIn("bge-m3", text)
        self.assertIn("python sysreport.py --bench", text)

    def test_bench_measures_text_and_vision_and_skips_embeddings(self):
        server = FakeOllama({"gemma3:4b": ["completion", "vision"], "qwen3:8b": ["completion", "thinking"],
                             "bge-m3": ["embedding"]},
                            responder=lambda payload: {"text": "красный и синий"}).start()
        self.addCleanup(server.stop)
        text = "\n".join(sysreport.build_report(server.url, True, 5))
        self.assertIn("ЗАМЕР СКОРОСТИ", text)
        self.assertIn("gemma3:4b", text)
        self.assertIn("ЧИТАЕТ", text)
        measured = [p for path, p in server.requests if path == "/api/chat" and p.get("messages")]
        self.assertFalse([p for p in measured if p["model"] == "bge-m3"])
        thinking = [p for p in measured if p["model"] == "qwen3:8b"]
        self.assertTrue(thinking and all(p.get("think") is False for p in thinking))
        plain = [p for p in measured if p["model"] == "gemma3:4b"]
        self.assertTrue(all("think" not in p for p in plain))
        unloads = [p for path, p in server.requests if path == "/api/chat" and p.get("keep_alive") == 0]
        self.assertEqual({p["model"] for p in unloads}, {"gemma3:4b", "qwen3:8b"})

    def test_bench_error_is_reported_per_model(self):
        server = FakeOllama({"gemma3:4b": ["completion"]}).start()
        self.addCleanup(server.stop)
        server.script({"status": 500, "error": "model requires more system memory"})
        text = "\n".join(sysreport.build_report(server.url, True, 5))
        self.assertIn("ОШИБКА", text)
        self.assertIn("more system memory", text)

    def test_report_has_no_personal_data(self):
        import getpass
        import platform
        server = FakeOllama().start()
        self.addCleanup(server.stop)
        text = "\n".join(sysreport.build_report(server.url, False, 3))
        for secret in (getpass.getuser(), platform.node(), server.url):
            if len(secret) > 3:
                self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()

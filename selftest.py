"""Самопроверка: python selftest.py

Запускает все автотесты из папки tests/ без внешних зависимостей (только стандартная
библиотека): Ollama и файлы подменяются поддельными. Тесты окна нужен дисплей —
на машине без экрана они пропускаются (на сервере: ``xvfb-run -a python selftest.py``).

    python selftest.py            все тесты
    python selftest.py -k llm     только подходящие по имени
    python selftest.py -v         подробно
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):              # Windows при перенаправлении: cp1252 не знает кириллицу
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    verbosity = 2 if "-v" in argv else 1
    pattern = ""
    if "-k" in argv and argv.index("-k") + 1 < len(argv):
        pattern = argv[argv.index("-k") + 1].lower()
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))

    def flatten(tests):
        for item in tests:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item

    cases = [t for t in flatten(suite) if not pattern or pattern in t.id().lower()]
    result = unittest.TextTestRunner(verbosity=verbosity).run(unittest.TestSuite(cases))
    skipped = len(result.skipped)
    print(f"\nтестов: {result.testsRun}, пропущено: {skipped}, ошибок: {len(result.errors)}, "
          f"падений: {len(result.failures)}")
    if skipped and not result.failures and not result.errors:
        print("(пропущены тесты окна — нет дисплея; на сервере: xvfb-run -a python selftest.py)")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

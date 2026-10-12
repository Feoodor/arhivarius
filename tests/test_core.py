"""Ядро без модели: чтение файлов, правила и страховки имён, карта, перенос и откат, конфиг."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from core import applier, mapio
from core.config_loader import load_config, slice_budget
from core.extract import Extracted, decode_text, extract, pick_dates, read_text_file
from core.prompts import DOC_TYPES, build_system, parse_answer
from core.rules import (Verdict, build_record, canon_type_block, clean_file_name, iso_field,
                        resolve_path)
from tests.helpers import make_archive, scan_and_index

TEXT = "Письмо 12. Приглашаем вас на слёт 17 июля 2026 года. " * 200


class Decoding(unittest.TestCase):
    def test_every_common_encoding_and_every_truncation(self):
        """Регресс: UTF-8, оборванный посреди буквы, читался как cp1251 (мусор); UTF-16/cp866/koi8-r — тоже."""
        variants = {
            "utf-8": TEXT.encode("utf-8"),
            "utf-8-bom": b"\xef\xbb\xbf" + TEXT.encode("utf-8"),
            "utf-16 BOM": b"\xff\xfe" + TEXT.encode("utf-16-le"),
            "utf-16-be без BOM": TEXT.encode("utf-16-be"),
            "utf-16-le без BOM": TEXT.encode("utf-16-le"),
            "cp1251": TEXT.replace("№", "N").encode("cp1251"),
            "cp866": TEXT.encode("cp866"),
            "koi8-r": TEXT.encode("koi8-r"),
            "cp1251 ЗАГЛАВНЫМИ": TEXT.upper().encode("cp1251"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, raw in variants.items():
                path = Path(tmp) / "f.txt"
                path.write_bytes(raw)
                for limit in range(200, 260):               # разные места обрыва
                    got = read_text_file(path, limit)
                    self.assertTrue(got.casefold().startswith("письмо"), f"{name}, limit={limit}: {got[:40]!r}")

    def test_plain_ascii_and_garbage_do_not_crash(self):
        self.assertEqual(decode_text(b""), "")
        self.assertEqual(decode_text(b"Hello 123"), "Hello 123")
        self.assertIsInstance(decode_text(bytes(range(256)) * 4), str)


class ReviewDecoding(unittest.TestCase):
    """Находки независимого ревью (чтение текста)."""

    def test_belarusian_and_ukrainian_cp1251(self):
        be = "Гэта ліст ад 12 сакавіка 2026 года. Запрашаем вас на злёт, дзе будуць ўсе. " * 20
        uk = "Це лист від 12 березня 2026 року. Запрошуємо вас на зліт, де будуть усі: ґанок, їжак, єнот. " * 20
        for text in (be, uk):
            self.assertEqual(decode_text(text.encode("cp1251")), text)

    def test_short_cp1251_file_is_not_clipped(self):
        """Хвост отбрасывается только у файла, реально обрезанного лимитом чтения."""
        self.assertEqual(decode_text("ID: 5 по".encode("cp1251")), "ID: 5 по")
        self.assertEqual(decode_text("ID: 5 по".encode("cp1251"), truncated=False), "ID: 5 по")

    def test_rtf_emoji_and_broken_escapes_never_crash_or_leave_surrogates(self):
        bs = chr(92)
        emoji = f"{bs}u-10179?{bs}u-8704?"                        # так Word пишет 😀
        rtf = ("{" + bs + "rtf1" + bs + "ansi hello " + emoji + " world " + bs + "u99999999? "
               + bs + "ansicpg10000 " + bs + "'zz " + bs + "\xe9 end}")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.rtf"
            path.write_bytes(rtf.encode("latin-1"))
            res = extract(path, 3000)
            self.assertIn("😀", res.text)
            self.assertIn("world", res.text)
            res.text.encode("utf-8")                              # не должно быть суррогатов

    def test_docx_self_closing_del_does_not_eat_real_text(self):
        body = ('<w:p><w:r><w:t>Приказ 5</w:t></w:r></w:p>'
                '<w:p><w:pPr><w:rPr><w:del w:id="1" w:author="x"/></w:rPr></w:pPr><w:r><w:t>Важный абзац</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>Ещё один с датой 12.03.2026.</w:t></w:r></w:p>'
                '<w:p><w:del w:id="2"><w:r><w:delText>УДАЛЕНО</w:delText></w:r></w:del><w:r><w:t>Конец.</w:t></w:r></w:p>')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.docx"
            make_docx(path, body)
            got = extract(path, 3000).text
            for must in ("Приказ 5", "Важный абзац", "Ещё один с датой 12.03.2026", "Конец."):
                self.assertIn(must, got)
            self.assertNotIn("УДАЛЕНО", got)

    def test_odt_keeps_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.odt"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("content.xml", "<text:p>слово<text:s/>слово</text:p>")
            self.assertIn("слово слово", extract(path, 3000).text)


class ReviewMap(unittest.TestCase):
    def row(self, path="a.txt", **kw):
        base = {k: "" for k in mapio.MAP_FIELDS}
        base.update({"Текущий путь": path, "Применить": "FALSE", "Статус": "проверка"})
        base.update(kw)
        return base

    def test_registered_manual_edit_survives_rerun(self):
        """Человек поправил имя в окне просмотра (без комментария, статус «проверка») — повтор не затирает."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            edited = self.row("a.txt", **{"Новое имя": "моё-имя.txt", "Целевой путь": "Моя/папка"})
            plain = self.row("b.txt", **{"Новое имя": "старое.txt"})
            mapio.mark_manual(out, "a.txt")
            records = [{"Текущий путь": p, "Новое имя": "модель.txt", "Целевой путь": "X", "Статус": "проверка"}
                       for p in ("a.txt", "b.txt")]
            rows, added, updated, skipped = mapio.merge_suggestions(
                [edited, plain], records, manual=mapio.load_manual(out))
            by = {r["Текущий путь"]: r for r in rows}
            self.assertEqual(by["a.txt"]["Новое имя"], "моё-имя.txt")
            self.assertEqual(by["b.txt"]["Новое имя"], "модель.txt")
            self.assertEqual((updated, skipped), (1, 1))

    def test_manual_registry_roundtrip_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(mapio.load_manual(out), set())
            mapio.mark_manual(out, "x\\y.txt")
            mapio.mark_manual(out, "x/y.txt")
            self.assertEqual(mapio.load_manual(out), {"x/y.txt"})
            (out / mapio.MANUAL_NAME).write_text("{не json", "utf-8")
            self.assertEqual(mapio.load_manual(out), set())


class ReviewModels(unittest.TestCase):
    def test_pick_best_avoids_tiny_and_code_models(self):
        from core.models import pick_best

        def lst(*names):
            return [{"name": n, "size": 5 * 10 ** 9} for n in names]

        self.assertEqual(pick_best(lst("gemma3:1b", "qwen3:14b"), vision=False), "qwen3:14b")
        self.assertEqual(pick_best(lst("qwen3-coder:30b", "mistral:7b"), vision=False), "mistral:7b")
        self.assertEqual(pick_best(lst("nomic-embed-text", "bge-m3"), vision=False), "")

    def test_cloud_models_are_never_picked_automatically(self):
        from core.models import is_cloud, pick_best
        self.assertTrue(is_cloud("minimax-m3:cloud"))
        self.assertTrue(is_cloud("qwen3-coder:480b-cloud"))
        self.assertFalse(is_cloud("gemma3:4b"))
        self.assertFalse(is_cloud("cloudy-model:7b"))
        installed = [{"name": "minimax-m3:cloud", "size": 0}, {"name": "gemma3:4b", "size": 3 * 10 ** 9}]
        self.assertEqual(pick_best(installed, vision=False), "gemma3:4b")
        self.assertEqual(pick_best(installed[:1], vision=False), "")
        self.assertEqual(pick_best([{"name": "qwen3.5:9b", "size": 6 * 10 ** 9},
                                    {"name": "gemma3:4b", "size": 3 * 10 ** 9}], vision=False), "gemma3:4b")


def make_docx(path: Path, body: str, footer: str = "", extra_document_xml: str = "") -> None:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = f'<w:document {ns}><w:body>{body}{extra_document_xml}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", document)
        if footer:
            z.writestr("word/footer1.xml", f'<w:ftr {ns}><w:p><w:r><w:t>{footer}</w:t></w:r></w:p></w:ftr>')


class Office(unittest.TestCase):
    def test_docx_reads_footer_and_ignores_fields_and_deleted_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.docx"
            body = ('<w:p><w:r><w:t>ПРИКАЗ</w:t></w:r></w:p>'
                    '<w:p><w:r><w:instrText> HYPERLINK "http://x" </w:instrText></w:r><w:r><w:t>Тело</w:t></w:r></w:p>'
                    '<w:p><w:del><w:r><w:delText>УДАЛЕНО</w:delText></w:r></w:del><w:r><w:t>остаётся</w:t></w:r></w:p>')
            make_docx(path, body, footer="Исх-77 от 01.02.2026")
            got = extract(path, 3000).text
            self.assertIn("ПРИКАЗ", got)
            self.assertIn("Исх-77 от 01.02.2026", got, "колонтитул не прочитан")
            self.assertNotIn("HYPERLINK", got)
            self.assertNotIn("УДАЛЕНО", got)
            self.assertIn("остаётся", got)

    def test_rtf_with_cyrillic_escapes_and_unicode(self):
        bs = chr(92)                    # обратная косая: код символа нельзя писать литералом — его раскроет сам Python
        unicode_word = "".join(f"{bs}u{code}?" for code in (1056, 1072, 1087, 1086, 1088, 1090))   # «Рапорт»
        rtf = (r"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
               r"{\*\generator Word}\pard \'cf\'f0\'e8\'e3\'eb\'e0\'f8\'e5\'ed\'e8\'e5\par "
               + unicode_word + r"\tab 2026\par}")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.rtf"
            path.write_bytes(rtf.encode("latin-1"))
            res = extract(path, 3000)
            self.assertIn("Приглашение", res.text)
            self.assertIn("Рапорт", res.text)
            self.assertNotIn("Arial", res.text)
            self.assertNotIn("generator", res.text.lower())
            self.assertFalse(res.force_review)

    def test_odt_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.odt"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("content.xml", "<office:text><text:p>Программа слёта</text:p><text:p>17 июля 2026</text:p></office:text>")
            self.assertIn("Программа слёта\n17 июля 2026", extract(path, 3000).text)

    def test_broken_files_never_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, data in (("bad.docx", b"not a zip"), ("bad.xlsx", b"junk"), ("bad.pdf", b"%PDF-broken"),
                               ("bad.rtf", b"{\\rtf1 {{{"), ("bad.odt", b"x"), ("empty.txt", b"")):
                path = Path(tmp) / name
                path.write_bytes(data)
                res = extract(path, 3000)
                self.assertTrue(res.force_review or res.usable or res.kind in ("empty", "error", "legacy"), name)

    def test_dates_picked_from_russian_text(self):
        years, dates = pick_dates("Слёт пройдёт 17 июля 2026 года, приказ от 12.03.2026 и 2025-12-31")
        self.assertIn("2026-07-17", dates)
        self.assertIn("2026-03-12", dates)
        self.assertIn("2025-12-31", dates)
        self.assertEqual(years, ["2025", "2026"])


class Naming(unittest.TestCase):
    def test_clean_file_name(self):
        cases = {
            "2026-03-12 Вх-12 Письмо.docx": "2026-03-12_Вх-12_Письмо.docx",
            "Рапорт №5 (финал).docx": "Рапорт_5.docx",
            "2026-05-09_Рапорт_Игра-9-мая.DOCX": "2026-05-09_Рапорт_Игра-9-мая.docx",
            "a/b\\c:d*e?.txt": "cde.txt",
            "  ": "Безымянный.txt",
            "2025-Отчёт.pdf": "2025_Отчёт.pdf",
        }
        for raw, want in cases.items():
            ext = Path(raw.replace("\\", "/")).suffix.lower() or ".txt"
            got = clean_file_name(raw, ext)
            self.assertEqual(got, want, raw)

    def test_name_has_no_forbidden_chars_and_bounded_length(self):
        got = clean_file_name('<>:"|?*' + "а" * 400 + ".docx", ".docx")
        self.assertLessEqual(len(got), 130)
        self.assertTrue(got.endswith(".docx"))
        self.assertFalse(set('<>:"/\\|?*') & set(got))

    def test_type_block_capitalised(self):
        self.assertEqual(canon_type_block("2026-03-12_рапорт_Тема.docx"), "2026-03-12_Рапорт_Тема.docx")
        self.assertEqual(canon_type_block("2026-03-12_отчет_Тема.docx"), "2026-03-12_Отчёт_Тема.docx")

    def test_iso_field(self):
        for raw, want in (("2026-07-17", "2026-07-17"), ("17.07.2026", "2026-07-17"), ("2026-07", "2026-07"),
                          ("2026", "2026"), ("2026-13-01", ""), ("вчера", ""), ("", ""), (None, "")):
            self.assertEqual(iso_field(raw), want, raw)

    def test_doc_types_listed_in_prompt(self):
        for word in ("Рапорт", "Письмо", "Протокол", "Афиша"):
            self.assertIn(word, DOC_TYPES)


class Parsing(unittest.TestCase):
    def test_parse_answer_variants(self):
        good = {"new_name": "a.docx", "target_path": "X/Y"}
        for text in (json.dumps(good), "```json\n" + json.dumps(good) + "\n```", "Вот ответ:\n" + json.dumps(good) + "\nГотово",
                     '{"new_name": "a.docx", "target_path": "X/Y"'):          # обрезан num_predict
            self.assertEqual(parse_answer(text)["new_name"], "a.docx", text)

    def test_parse_answer_rejects_non_json(self):
        for text in ("", "   ", "нет json вообще"):
            with self.assertRaises(ValueError):
                parse_answer(text)


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config, self.root = make_archive(Path(self.tmp.name), {"a.txt": "текст"})
        self.snapshot, self.index = scan_and_index(self.config)
        self.item = next(i for i in self.snapshot["files"] if i["path"].endswith("a.txt"))

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, answer: dict, text: str = "Письмо Вх-123 от 12.03.2026", years=("2026",)):
        extracted = Extracted(text=text, kind="text", years=list(years), dates=["2026-03-12"])
        base = {"name_confidence": 0.95, "move_confidence": 0.95, "review": False}
        return build_record(self.item, extracted, {**base, **answer}, self.index, self.config, self.root, [], {})

    def test_clean_answer_is_ready(self):
        v = self.record({"new_name": "2026-03-12_Вх-123_Письмо_Встреча.txt", "direction": "вх", "reg_number": "123",
                         "doc_date": "2026-03-12", "target_path": "Документы/Входящие/2026"})
        self.assertEqual(v.record["Статус"], "готово", v.flags)
        self.assertEqual(v.record["Целевой путь"], "Документы/Входящие/2026")

    def test_invented_year_is_flagged(self):
        v = self.record({"new_name": "2031-03-12_Письмо_Встреча.txt", "doc_date": "2031-03-12",
                         "target_path": "Документы/Входящие/2026"})
        self.assertEqual(v.record["Статус"], "проверка")
        self.assertTrue(any("не подтверждён" in f or "не подтверждена" in f for f in v.flags), v.flags)

    def test_invented_folder_caps_move_confidence(self):
        v = self.record({"new_name": "2026-03-12_Письмо_Встреча.txt", "doc_date": "2026-03-12",
                         "target_path": "Документы/Входящие/2026/Выдуманная-папка"})
        self.assertLessEqual(v.record["Уверенность переноса"], 0.8)
        self.assertEqual(v.record["Статус"], "проверка")

    def test_file_name_stripped_from_target(self):
        v = self.record({"new_name": "2026-03-12_Письмо_Встреча.txt", "doc_date": "2026-03-12",
                         "target_path": "Документы/Входящие/2026/2026-03-12_Письмо_Встреча.txt"})
        self.assertEqual(v.record["Целевой путь"], "Документы/Входящие/2026")

    def test_wrong_root_prefix_is_repaired(self):
        v = self.record({"new_name": "2026-03-12_Письмо_Встреча.txt", "doc_date": "2026-03-12",
                         "target_path": "Входящие/2026"})
        self.assertEqual(v.record["Целевой путь"], "Документы/Входящие/2026")

    def test_case_and_yo_tolerated_in_target(self):
        v = self.record({"new_name": "2026-03-12_Письмо_Встреча.txt", "doc_date": "2026-03-12",
                         "target_path": "документы/входящие/2026"})
        self.assertEqual(v.record["Целевой путь"], "Документы/Входящие/2026")

    def test_extension_is_forced_back(self):
        v = self.record({"new_name": "2026-03-12_Письмо_Встреча.docx", "doc_date": "2026-03-12",
                         "target_path": "Документы/Входящие/2026"})
        self.assertTrue(v.record["Новое имя"].endswith(".txt"))

    def test_discard_goes_to_trash_never_deleted(self):
        v = self.record({"new_name": "a.txt", "target_path": "", "discard": True, "discard_reason": "пусто"})
        self.assertEqual(v.record["Тип операции"], "в корзину")
        self.assertEqual(v.record["Статус"], "проверка")

    def test_missing_confidence_means_review(self):
        v = self.record({"new_name": "2026-03-12_Письмо_Встреча.txt", "target_path": "Документы/Входящие/2026",
                         "name_confidence": 0, "move_confidence": 0})
        self.assertEqual(v.record["Статус"], "проверка")

    def test_garbage_answer_does_not_crash(self):
        for answer in ({}, {"new_name": None, "target_path": 5, "name_confidence": "много"},
                       {"new_name": "x", "direction": "???", "doc_date": "когда-нибудь", "event_start": "2026-99-99"}):
            v = self.record(answer)
            self.assertEqual(v.record["Статус"], "проверка")

    def test_resolve_path_near_miss(self):
        v = Verdict()
        self.assertEqual(resolve_path("Документы/Прошения - Рапорты/2026", self.index, v),
                         "Документы/Прошения - Рапорты/2026")
        self.assertEqual(resolve_path("документы/прошения - рапорты/2026", self.index, Verdict()),
                         "Документы/Прошения - Рапорты/2026")


class Map(unittest.TestCase):
    def row(self, **kw):
        base = {k: "" for k in mapio.MAP_FIELDS}
        base.update({"Текущий путь": "a/b.txt", "Применить": "FALSE", "Статус": "проверка"})
        base.update(kw)
        return base

    def test_rerun_updates_untouched_rows_but_keeps_human_edits(self):
        """Регресс: «Применить=FALSE» считалось правкой человека — повторный разбор ничего не обновлял."""
        untouched = self.row(**{"Новое имя": "старое.txt"})
        commented = self.row(**{"Текущий путь": "c.txt", "Комментарий": "не туда кладёшь", "Новое имя": "моё.txt"})
        marked = self.row(**{"Текущий путь": "d.txt", "Применить": "TRUE", "Новое имя": "утверждено.txt"})
        records = [{"Текущий путь": p, "Новое имя": "новое.txt", "Статус": "проверка", "Целевой путь": "X"}
                   for p in ("a/b.txt", "c.txt", "d.txt")]
        rows, added, updated, skipped = mapio.merge_suggestions([untouched, commented, marked], records)
        by = {r["Текущий путь"]: r for r in rows}
        self.assertEqual(by["a/b.txt"]["Новое имя"], "новое.txt")
        self.assertEqual(by["c.txt"]["Новое имя"], "моё.txt")
        self.assertEqual(by["d.txt"]["Новое имя"], "утверждено.txt")
        self.assertEqual((added, updated, skipped), (0, 1, 2))

    def test_csv_roundtrip_survives_excel_hazards(self):
        nasty = self.row(**{"Причина": 'цитата "в кавычках"; точка с запятой\nи перевод строки', "Новое имя": "а;б.txt"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.csv"
            mapio.save_map(path, [nasty])
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"), "нужен BOM для Excel")
            back = mapio.load_map(path)
            self.assertEqual(back[0]["Причина"], nasty["Причина"])
            self.assertEqual(back[0]["Новое имя"], "а;б.txt")


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config, self.root = make_archive(Path(self.tmp.name), {"a.txt": "один", "b.txt": "два"})
        self.src = self.root / "Документы/00_Разобрать"

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, name: str, new: str, target: str = "Документы/Входящие/2026", **kw):
        base = {k: "" for k in mapio.MAP_FIELDS}
        base.update({"Текущий путь": f"Документы/00_Разобрать/{name}", "Новое имя": new, "Целевой путь": target,
                     "Применить": "TRUE", "Статус": "готово"})
        base.update(kw)
        return base

    def test_move_rename_undo_roundtrip_and_nothing_is_deleted(self):
        rows = [self.row("a.txt", "2026-01-01_Письмо_А.txt")]
        before = sorted(p.read_text("utf-8") for p in self.root.rglob("*.txt"))
        result = applier.apply_plan(self.config, rows)
        self.assertEqual((result["перенесено"], result["ошибок"]), (1, 0))
        moved = self.root / "Документы/Входящие/2026/2026-01-01_Письмо_А.txt"
        self.assertTrue(moved.exists())
        self.assertFalse((self.src / "a.txt").exists())
        back = applier.undo_last(self.config)
        self.assertEqual((back["возвращено"], back["ошибок"]), (1, 0))
        self.assertTrue((self.src / "a.txt").exists())
        self.assertEqual(sorted(p.read_text("utf-8") for p in self.root.rglob("*.txt")), before)

    def test_name_clash_gets_suffix_never_overwrites(self):
        existing = self.root / "Документы/Входящие/2026"
        (existing / "2026-01-01_Письмо.txt").write_text("СТАРОЕ", "utf-8")
        rows = [self.row("a.txt", "2026-01-01_Письмо.txt"), self.row("b.txt", "2026-01-01_Письмо.txt")]
        applier.apply_plan(self.config, rows)
        names = sorted(p.name for p in existing.glob("*.txt"))
        self.assertEqual(len(names), 3, names)
        self.assertEqual((existing / "2026-01-01_Письмо.txt").read_text("utf-8"), "СТАРОЕ")

    def test_unmarked_reserved_and_escaping_rows_are_safe(self):
        outside = self.root.parent / "вне_корня"
        rows = [self.row("a.txt", "x.txt", Применить="FALSE"),
                self.row("b.txt", "y.txt", target="../../вне_корня"),
                self.row("a.txt", "NUL.txt"),
                self.row("a.txt", "")]
        applier.apply_plan(self.config, rows)
        self.assertTrue((self.src / "a.txt").exists(), "неотмеченная/зарезервированная строка сдвинула файл")
        self.assertFalse(outside.exists(), "файл вышел за пределы корня")
        # «..» отбрасывается, файл остаётся внутри корня
        inside = list(self.root.rglob("y.txt"))
        self.assertTrue(all(self.root in p.parents for p in inside))

    def test_case_only_rename_and_missing_source(self):
        rows = [self.row("a.txt", "A.txt", target="Документы/00_Разобрать"),
                self.row("исчез.txt", "z.txt")]
        result = applier.apply_plan(self.config, rows)
        self.assertEqual(result["ошибок"], 0)
        self.assertIn("A.txt", [p.name for p in self.src.iterdir()])

    def test_safe_target_keeps_requested_case(self):
        """Регресс (Windows): resolve() подменял регистр имени на дисковый → «a.txt»→«A.txt» давало «a_v2.txt»."""
        (self.src / "a.txt").write_text("x", "utf-8")
        target = applier._safe_target(self.root, "Документы/00_Разобрать/A.txt")
        self.assertEqual(target.name, "A.txt")
        for bad in ("../x.txt", "Документы/..", "Документы/00_Разобрать/.."):
            with self.assertRaises(ValueError):
                applier._safe_target(self.root, bad)

    def test_journal_lets_undo_work_even_after_a_crash(self):
        """Журнал отката пишется по ходу дела: падение посреди пачки не оставляет файлы без записи."""
        rows = [self.row("a.txt", "1.txt"), self.row("b.txt", "2.txt")]
        applier.apply_plan(self.config, rows)
        journals = list(self.config["output_path"].glob("undo_*"))
        self.assertTrue(journals)

    def test_long_windows_path_is_refused_before_moving(self):
        long_target = "Документы/Входящие/2026/" + "/".join(["очень-длинная-папка-" + "я" * 40] * 6)
        rows = [self.row("a.txt", "x.txt", target=long_target)]
        from unittest import mock
        with mock.patch.object(applier, "WINDOWS", True), mock.patch.object(applier, "MAX_PATH", 120):
            result = applier.apply_plan(self.config, rows)
        self.assertEqual((result["запланировано"], result["перенесено"]), (0, 0))
        self.assertTrue((self.src / "a.txt").exists())

    def test_missing_output_dir_does_not_break_a_move(self):
        """Регресс: журнал отката падал ПОСЛЕ переноса, и успешный перенос считался ошибкой без записи для отката."""
        import shutil
        shutil.rmtree(self.config["output_path"], ignore_errors=True)
        result = applier.apply_plan(self.config, [self.row("a.txt", "1.txt")])
        self.assertEqual((result["перенесено"], result["ошибок"]), (1, 0))
        self.assertEqual(applier.undo_last(self.config)["возвращено"], 1)


class Config(unittest.TestCase):
    def test_broken_config_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text("{не json", "utf-8")
            with self.assertRaises(RuntimeError) as cm:
                load_config(Path(tmp))
            self.assertIn("повреждён", str(cm.exception))

    def test_defaults_are_filled_and_garbage_values_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text(json.dumps(
                {"llm": {"num_ctx": "много", "base_url": "localhost:11434"}}), "utf-8")
            config = load_config(Path(tmp))
            self.assertIsInstance(config["llm"]["num_ctx"], int)
            self.assertTrue(config["llm"]["base_url"].startswith("http"))
            self.assertTrue(config["llm"]["model_text"])

    def test_context_budget_shrinks_with_window(self):
        big = slice_budget({"llm": {"num_ctx": 32768, "content_chars": 3000}, "folders": {"budget_chars": 22000}}, 5000)
        small = slice_budget({"llm": {"num_ctx": 8192, "content_chars": 3000}, "folders": {"budget_chars": 22000}}, 5000)
        self.assertEqual(big, 22000)
        self.assertLess(small, big)
        self.assertGreater(small, 0)

    def test_system_prompt_mentions_roots_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = make_archive(Path(tmp))
            system = build_system(config)
            self.assertIn("Входящие", system)
            self.assertIn("Рапорт", system)
            self.assertNotIn("{roots}", system)
            self.assertNotIn("{doc_types}", system)


if __name__ == "__main__":
    unittest.main()

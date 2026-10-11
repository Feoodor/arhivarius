"""Точка входа «Архивариус»: графическое окно и командная строка.

Без аргументов открывается окно. Команды нужны для скриптов и для того, чтобы
увидеть, что именно уйдёт в модель:

    python app.py doctor                проверка диска, Ollama, модели и бюджетов
    python app.py scan                  снимок диска (только чтение)
    python app.py context --path "Документы/..."   показать промпт одного файла
    python app.py suggest --limit 5 --prefix "Документы/00_Разобрать"
    python app.py folder --path "Документы/00_Разобрать" --vision   папка целиком
    python app.py lessons               вынести уроки из правок в learned_rules.md
    python app.py refine --path "Документы/..." --comment "не туда кладёшь"
    python app.py merge                 предложения -> migration_map.csv
    python app.py dry-run | apply | undo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if getattr(sys, "frozen", False):                 # PyInstaller: рядом с exe, не в _MEIPASS
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from core import applier, mapio, scanner  # noqa: E402
from core.config_loader import MAP_NAME, SNAPSHOT_NAME, SUGGESTIONS_NAME, load_config, save_config  # noqa: E402
from core.extract import extract  # noqa: E402
from core.folders import FolderIndex, keywords_from_text  # noqa: E402
from core.pipeline import Analyzer, Filters  # noqa: E402
from core.prompts import build_system, build_user  # noqa: E402

OPTIONAL_LIBS = (
    ("fitz", "PyMuPDF — чтение текстовых PDF"),
    ("openpyxl", "openpyxl — чтение XLSX"),
    ("PIL", "Pillow — метаданные и сжатие изображений"),
)


def say(message: str = "") -> None:
    print(message, flush=True)


def hr(title: str) -> None:
    say("\n" + title.upper() + "\n" + "-" * max(12, len(title)))


# ---------------------------------------------------------------------- doctor
def estimate_prompt_tokens(config: dict) -> dict:
    system = build_system(config)
    system_tokens = int(len(system) / 2.6)
    budget = config["folders"].get("budget_chars_effective") or config["folders"]["budget_chars"]
    folders = int(budget / 2.6)
    content = int(config["llm"]["content_chars"] / 2.6)
    return {
        "system": system_tokens,
        "folders": folders,
        "content": content,
        "total": system_tokens + folders + content + 220,
    }


def cmd_doctor(config: dict) -> int:
    from core.llm import client_from_config

    problems = list(config.get("problems", []))
    hr("Проверка готовности")
    say(f"  Python            {sys.version.split()[0]}")
    for label, value in (("Корень отдела", config["mo_root"]), ("Сканируется", config["scan_root"]),
                         ("Служебная папка", config["output_path"])):
        say(f"  {label:<18} {value}")
        if not Path(str(value)).exists():
            problems.append(f"папка не найдена: {value}")

    for module, purpose in OPTIONAL_LIBS:
        try:
            __import__(module)
            say(f"  {module:<18} ок")
        except Exception:
            say(f"  {module:<18} НЕТ — {purpose}")
            problems.append(f"нет библиотеки {module}: {purpose}")

    client = client_from_config(config, "text")
    if not client.is_alive():
        say(f"  Ollama            НЕ ОТВЕЧАЕТ на {config['llm']['base_url']}")
        problems.append("Ollama не запущена — откройте приложение Ollama (он слушает 127.0.0.1:11434)")
    else:
        details = client.list_models_detailed()
        models = [d["name"] for d in details]
        say(f"  Ollama            {client.base_url}, v{client.version() or '?'}, моделей: {len(models)}")
        for role, wanted in (("текст", config["llm"]["model_text"]), ("зрение", config["llm"]["model_vision"])):
            have = client.resolve_model(wanted)
            if not have:
                say(f"  модель {role:<10} {wanted} — НЕТ (ollama pull {wanted})")
                problems.append(f"модель {wanted} не скачана")
                continue
            caps = client.capabilities(have)
            limit = client.context_limit(have)
            say(f"  модель {role:<10} {have} — на месте"
                + (f" · {', '.join(caps)}" if caps else "")
                + (f" · контекст до {limit}" if limit else ""))
            if role == "зрение" and config["llm"].get("vision") and caps and "vision" not in caps:
                problems.append(f"модель {have} не читает картинки: выберите другую в настройках «Модель для зрения»")
            if limit and int(config["llm"]["num_ctx"]) > limit:
                problems.append(f"num_ctx={config['llm']['num_ctx']} больше предела модели {have} ({limit})")
        from core import models as models_mod
        absent = models_mod.missing_recommended(models)
        if absent and not any(models_mod.same_model(config['llm']['model_text'], n) for n in models):
            best = models_mod.pick_best(details, vision=False, client=client)
            if best:
                say(f"  подойдёт вместо неё: {best} (программа возьмёт её сама, но лучше скачать выбранную)")
        loaded = client.loaded()
        if loaded:
            say("  в памяти          " + ", ".join(
                f"{m.get('name')} ~{int(m.get('size', 0)) / 1e9:.1f} ГБ" for m in loaded))

    snapshot = config["output_path"] / SNAPSHOT_NAME
    if snapshot.exists():
        data = json.loads(snapshot.read_text("utf-8"))
        say(f"  снимок            папок {len(data.get('folders', []))}, файлов {len(data.get('files', []))}, "
            f"от {data.get('created')}")
    else:
        say("  снимок            отсутствует — выполните scan")

    map_path = config["output_path"] / MAP_NAME
    if map_path.exists():
        stats = mapio.stats(mapio.load_map(map_path))
        say(f"  карта             строк {stats['всего']}, готово {stats['готово']}, "
            f"проверка {stats['проверка']}, отмечено {stats['отмечено']}")

    est = estimate_prompt_tokens(config)
    num_ctx = int(config["llm"]["num_ctx"])
    say(f"  бюджет промпта    правила ~{est['system']} + папки ~{est['folders']} + текст ~{est['content']} "
        f"= ~{est['total']} токенов при num_ctx={num_ctx}")
    if est["total"] > num_ctx * 0.9:
        problems.append(
            f"оценка промпта ({est['total']}) близка к num_ctx ({num_ctx}) — "
            "уменьшите folders.budget_chars / llm.content_chars или поднимите num_ctx"
        )

    hr("Итог")
    if problems:
        for item in problems:
            say("  ! " + item)
        return 1
    say("  всё в порядке — можно разбирать")
    return 0


# ----------------------------------------------------------------------- scan
def cmd_scan(config: dict) -> int:
    hr("Снимок диска")
    say(f"  обход {config['scan_root']} …")

    def progress(name: str, count: int) -> None:
        print(f"\r  {count} файл(ов), сейчас: {name[:58]}", end="", flush=True)

    snapshot = scanner.scan(config, progress=progress)
    print()
    stats = snapshot["stats"]
    say(f"  папок {len(snapshot['folders'])}, файлов {len(snapshot['files'])}, архивов {len(snapshot['archives'])}")
    say(f"  захешировано заново {stats['захешировано_заново']}, взято из прошлого снимка {stats['хешей_взято_из_прошлого_снимка']}")
    return 0


def cmd_duplicates(config: dict) -> int:
    hr("Дубликаты по содержимому")
    report = scanner.duplicates_report(config)
    say(f"  групп {report['групп']}, лишних копий {report['лишних_копий']}, "
        f"повторно занято {report['байт_на_повторах'] / 1e6:.1f} МБ")
    say(f"  отчёт (ничего не удалено): {report['файл']}")
    return 0


def cmd_models(config: dict) -> int:
    from core import models as models_mod
    from core.llm import client_from_config

    client = client_from_config(config, "text")
    hr("Модели Ollama")
    if not client.is_alive():
        say("  Ollama не отвечает")
        return 1
    details = client.list_models_detailed()
    for item in details:
        caps = client.capabilities(item["name"])
        say(f"  {item['name']:<32} {int(item.get('size') or 0) / 1e9:5.1f} ГБ  {', '.join(caps)}")
    best = models_mod.pick_best(details, vision=True, client=client)
    if best:
        say(f"\n  лучшая установленная с глазами: {best}")
    absent = models_mod.missing_recommended([d["name"] for d in details])
    if absent:
        say("\n  можно скачать:")
        for rec in absent:
            say(f"    ollama pull {rec['tag']:<14} — {rec['role']}; {rec['why']}")
    return 0


# -------------------------------------------------------------------- context
def load_index(config: dict) -> tuple[dict, FolderIndex]:
    snapshot = scanner.load_snapshot(config)
    if not snapshot:
        raise SystemExit("нет снимка диска — сначала выполните scan")
    index = FolderIndex.from_snapshot(
        snapshot,
        prune=[*sorted(config["excluded_dirs"]), config["output_path"].name],
        exclude=config["folders"].get("hide_prefixes", []),
    )
    return snapshot, index


def cmd_context(config: dict, rel: str) -> int:
    hr("Промпт для одного файла")
    snapshot, _ = load_index(config)
    want = rel.replace("\\", "/")
    item = next((i for i in snapshot["files"] if str(i["path"]).replace("\\", "/") == want), None)
    if not item:
        say(f"  файл не найден в снимке: {want}")
        return 1
    from core.pipeline import build_prompt_preview, context_pack_for

    _, data, pack, _ = context_pack_for(config, want)
    preview = build_prompt_preview(config, want)
    est = estimate_prompt_tokens(config)
    say(f"  содержимое:  {data.kind}, символов {len(data.text)}, годы {data.years}, даты {data.dates[:4]}"
        + (f" | {data.note}" if data.note else ""))
    say(f"  срез папок:  {pack.summary}")
    say(f"  весь запрос: {len(preview)} символов, оценка по бюджету ~{est['total']} токенов "
        f"при num_ctx={config['llm']['num_ctx']}")
    say("\n  ---- ЧТО УХОДИТ В МОДЕЛЬ (первые 4000 символов) ----\n")
    say(preview[:4000])
    return 0


# -------------------------------------------------------------------- suggest
def cmd_suggest(config: dict, args) -> int:
    snapshot, index = load_index(config)
    filters = Filters(
        prefix=args.prefix if args.prefix is not None else str(config["llm"].get("default_prefix", "")),
        limit=int(args.limit or config["llm"].get("default_limit", 20)),
        include_media=bool(args.media),
        order=args.order or str(config["queue"].get("order", "shallow")),
        skip_done=not args.redo,
    )
    analyzer = Analyzer(config, snapshot, index, log=say)
    if args.vision:
        analyzer.vision_enabled = True
        analyzer.vision_client = analyzer.client
    records = analyzer.run(filters)
    if not records:
        say("  пусто: проверьте --prefix или выполните scan")
        return 1
    say("\n  ИТОГ ПАРТИИ")
    for row in (mapio.as_map_row(r) for r in records):
        conf = f"{row['Уверенность имени']}/{row['Уверенность переноса']}"
        say(f"   {row['Статус']:<9} {conf:<10} {row['Текущий путь'][:56]:<56} → "
            f"{row['Целевой путь'][:50]}/{row['Новое имя'][:34]}")
    say(f"\n  сохранено: {config['output_path'] / SUGGESTIONS_NAME}")
    if args.merge:
        cmd_merge(config)
    return 0


# ---------------------------------------------------------------------- merge
def cmd_merge(config: dict) -> int:
    hr("Перенос предложений в карту")
    records = mapio.load_records(config["output_path"] / SUGGESTIONS_NAME)
    if not records:
        say("  предложений нет — сначала suggest")
        return 1
    rows = mapio.load_map(config["output_path"] / MAP_NAME)
    rows, added, updated, skipped = mapio.merge_suggestions(rows, records, overwrite=False)
    mapio.save_map(config["output_path"] / MAP_NAME, rows)
    stats = mapio.stats(rows)
    say(f"  добавлено {added}, обновлено {updated}, пропущено (ваши правки) {skipped}")
    say(f"  карта: строк {stats['всего']} | готово {stats['готово']} | проверка {stats['проверка']} | "
        f"отмечено {stats['отмечено']}")
    say(f"  файл: {config['output_path'] / MAP_NAME}")
    return 0


# ------------------------------------------------------------------ dry/apply
def cmd_dry_run(config: dict) -> int:
    hr("Пробный перенос (ничего не меняется)")
    rows = mapio.load_map(config["output_path"] / MAP_NAME)
    if not rows:
        say("  карта пуста — выполните suggest и merge")
        return 1
    plan = applier.build_plan(config, rows)
    lines, counters = applier.describe(plan, config)
    say(f"  к переносу: {counters['всего']} файл(ов), {counters['байт'] / 1e6:.1f} МБ, "
        f"новых папок {counters['новых папок']}, конфликтов имён {counters['конфликтов']}")
    for line in lines[:120]:
        say("  " + line)
    if len(lines) > 120:
        say(f"  … и ещё {len(lines) - 120}")
    if not plan:
        say("  пусто: отметьте строки Применить=TRUE в карте (после глазной проверки) и повторите")
    return 0


def cmd_apply(config: dict, word: str) -> int:
    hr("Перенос")
    if word != "APPLY":
        say(f"  нужно подтвердить слово APPLY (получено {word!r}). Сначала dry-run и проверка карты.")
        return 1

    def progress(index: int, total: int, rel: str) -> None:
        print(f"\r  {index}/{total} {rel[:58]}", end="", flush=True)

    rows = mapio.load_map(config["output_path"] / MAP_NAME)
    result = applier.apply_plan(config, rows, progress=progress)
    print()
    say(f"  пачка {result['пачка']}: перенесено {result['перенесено']}, ошибок {result['ошибок']}")
    say(f"  журнал: {result['журнал']} (откат: python app.py undo --word UNDO)")
    return 0


def cmd_undo(config: dict, word: str) -> int:
    hr("Откат последней пачки")
    if word != "UNDO":
        say(f"  подтвердите слово UNDO (получено {word!r})")
        return 1

    def progress(index: int, total: int, rel: str) -> None:
        print(f"\r  {index}/{total}", end="", flush=True)

    say(f"  {applier.undo_last(config, progress=progress)}")
    print()
    return 0


def cmd_plugins(config: dict) -> int:
    from core import plugins

    hr("Возможности (плагины)")
    for row in plugins.status(config):
        mark = "установлено" if row["установлено"] else "нет"
        flag = "" if row["включено"] is None else (" · включено" if row["включено"] else " · выключено")
        say(f"  [{mark:>11}]{flag} {row['название']} — {row['зачем']}")
        if not row["установлено"]:
            say(f"                 как поставить: {row['команда']}")
    return 0


def cmd_plugin_install(config: dict, plugin_id: str) -> int:
    from core import plugins

    hr(f"Установка: {plugin_id}")
    ok = plugins.install(plugin_id, log=say, config=config)
    if ok:
        say("  готово. Не забудьте включить возможность на вкладке «Плагины» (или в config.json, раздел plugins).")
    return 0 if ok else 1


def cmd_plugin_check(config: dict, plugin_id: str) -> int:
    from core import plugins

    hr(f"Проверка: {plugin_id}")
    lines = plugins.verify(plugin_id, config, log=say)
    ready = all("НЕ найден" not in line and "нет —" not in line for line in lines)
    spec = next((p for p in plugins.registry() if p.id == plugin_id), None)
    if not ready and spec and spec.note:
        say("  нужно дополнительно: " + spec.note)
    return 0 if ready else 1


def cmd_broken(config: dict, limit: int) -> int:
    from core import plugins

    hr("Поиск битых файлов (только отчёт)")
    report = plugins.find_broken(config, limit=limit,
                                 progress=lambda name, count: print(f"\r  проверено {count}, сейчас: {name[:50]}", end="", flush=True))
    print()
    say(f"  проверено {report['проверено']}, подозрительных {report['подозрительных']}")
    say(f"  список: {report['файл']}")
    return 0


def cmd_convert(config: dict, apply: bool, limit: int) -> int:
    from core import plugins

    hr("Конвертация старых форматов (.doc, .rtf, .odt → .docx)")
    if apply:
        say("  оригиналы не удаляются и не изменяются: рядом создаётся .docx")
    result = plugins.convert_legacy(config, apply=apply, limit=limit,
                                    progress=lambda name, count: print(f"\r  {count}: {name[:56]}", end="", flush=True))
    print()
    say(f"  найдено {result['найдено']}, "
        + (f"конвертировано {result['конвертировано']}, " if apply else f"ждут конвертации {result['ждут']}, ")
        + f"ошибок {result['ошибок']}")
    say(f"  отчёт: {result['отчёт']}")
    if not apply:
        say("  это список. Чтобы выполнить, добавьте --apply")
    return 0


def cmd_repair_names(config: dict, apply: bool) -> int:
    from core import plugins

    hr("Починка имён с битой кодировкой")
    if apply:
        result = plugins.apply_name_repairs(config, log=say)
        say(f"  переименовано {result['переименовано']}, пропущено {result['пропущено']}")
        return 0
    rows = plugins.name_repairs(config)
    say(f"  предложений: {len(rows)}")
    for row in rows[:40]:
        say(f"   {row['объект']:<6} {row['где'][:38]:<38} {row['сейчас'][:34]:<34} → {row['предлагается'][:34]}")
    if len(rows) > 40:
        say(f"   … и ещё {len(rows) - 40}")
    say("  ничего не изменено. Чтобы применить, добавьте --apply")
    return 0


def cmd_structure(config: dict, args=None) -> int:
    from core import structure

    years = tuple(getattr(args, "years", None) or ())
    create = bool(getattr(args, "create", False))
    apply = bool(getattr(args, "apply", False))

    hr("Аудит структуры диска")
    snapshot, index = load_index(config)
    findings = structure.analyze(config, index)
    report = structure.render_report(config, findings)
    target = config["output_path"] / "structure_audit.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, "utf-8")
    say(f"  папок {findings['счётчик']['папок']}, файлов {findings['счётчик']['файлов']}")
    say(f"  разделов вне перечня: {len(findings['лишние_разделы'])}, пустых папок: {len(findings['пустые_папки'])}, "
        f"глубже 5 ярусов: {len(findings['слишком_глубоко'])}, одинаковых имён: {len(findings['дубли_названий'])}")
    say(f"  крупных свалок, просящихся в структуру: {len(findings['к_подъёму'])}")
    say(f"\n  отчёт: {target}")
    plan_file = structure.write_plan(config, structure.concrete_plan(config, years=years))
    say(f"  план папок (правьте его и запускайте --create): {plan_file}")
    paths = structure.read_plan(config)
    if create:
        result = structure.create_paths(config, paths, apply=apply, log=say)
        say(f"  {'создано' if apply else 'будет создано'}: {result['в плане']}, "
            f"уже есть: {result['уже есть']}, отказов: {result['отказов']}, отчёт: {result['отчёт']}")
        if not apply:
            say("  ничего не создано. Чтобы создать — добавьте --apply")
    return 0


def cmd_folder(config: dict, args=None) -> int:
    from core import folder_pass

    rel = str(getattr(args, "path", "") or "")
    hr(f"Папка целиком: {rel}")
    files = folder_pass.list_folder(config, rel)
    say(f"  файлов в папке (без подпапок): {len(files)}")
    result = folder_pass.folder_pass(config, rel, limit=int(getattr(args, "limit", 80)),
                                     vision=bool(getattr(args, "vision", False)), log=say)
    say(f"  предложений: {result.get('предложений', 0)}, новых строк карты: "
        f"{result.get('новых_строк_в_карте', 0)}, обновлено: {result.get('обновлено', 0)}")
    say("  все строки — со статусом «проверка»; откройте вкладку «Карта»")
    return 0


def cmd_lessons(config: dict, args=None) -> int:
    from core import lessons

    hr("Уроки из карты")
    text = lessons.distill(config, log=say)
    if text:
        say(text)
        say(f"\n  полный файл: {lessons.rules_path(config)}")
    return 0


def cmd_refine(config: dict, args=None) -> int:
    from core.refine import refine

    rel = str(getattr(args, "path", "") or "")
    hr(f"Глубокая перепроверка: {rel}")
    answer = refine(config, rel, comment=str(getattr(args, "comment", "") or ""))
    say(f"  имя:    {answer['new_name']}")
    say(f"  папка:  {answer['target_path']}")
    nc = answer.get("name_confidence", answer.get("confidence", 0))
    mc = answer.get("move_confidence", answer.get("confidence", 0))
    say(f"  уверенность: имя {nc:.2f} · папка {mc:.2f} · {answer['seconds']} с · {answer['model']}")
    say(f"  почему: {answer['reason']}")
    return 0


def cmd_settings(config: dict, pairs: list[str]) -> int:
    hr("Изменение настроек")
    for pair in pairs:
        if "=" not in pair:
            say(f"  пропускаю {pair!r} — нужно вида llm.num_ctx=8192")
            continue
        dotted, value = pair.split("=", 1)
        node = config
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        if not isinstance(node, dict) or keys[-1] not in node:
            say(f"  нет такого параметра: {dotted}")
            continue
        old = node[keys[-1]]
        if isinstance(old, bool):
            parsed: object = value.strip().lower() in {"1", "true", "yes", "да"}
        elif isinstance(old, (int, float)) and not isinstance(old, bool):
            parsed = float(value) if "." in value else int(value)
        elif isinstance(old, list):
            parsed = [item.strip() for item in value.split("|") if item.strip()]
        else:
            parsed = value
        node[keys[-1]] = parsed
        say(f"  {dotted} = {parsed}")
    say(f"  сохранено: {save_config(config)}")
    return 0


# ------------------------------------------------------------------------ gui
def cmd_gui(config: dict) -> int:
    try:
        from ui.main_window import OrganizerApp
    except Exception as exc:
        say(f"окно не открылось ({type(exc).__name__}: {exc}); команды CLI доступны — python app.py -h")
        return 1
    OrganizerApp(config).mainloop()
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Архивариус: разбор и переименование архива отдела (локально, через Ollama; регламент — в prompt.md)",
    )
    parser.add_argument("--config", default=str(APP_DIR / "config.json"), help="путь к config.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="показывать полный текст ошибки")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="графическое окно (по умолчанию)")
    sub.add_parser("doctor", help="проверка окружения и бюджетов контекста")
    sub.add_parser("scan", help="снимок диска")
    sub.add_parser("duplicates", help="отчёт о дубликатах по содержимому")
    sub.add_parser("merge", help="предложения -> карта переноса")
    sub.add_parser("dry-run", help="что будет сделано, без изменений")
    sub.add_parser("models", help="список моделей Ollama")
    sub.add_parser("plugins", help="состояние дополнительных возможностей")
    pchk = sub.add_parser("plugin-check", help="проверить библиотеки и внешнюю программу плагина")
    pchk.add_argument("plugin_id")
    pin = sub.add_parser("plugin-install", help="установить библиотеки плагина")
    pin.add_argument("plugin", help="id плагина из списка plugins")
    brk = sub.add_parser("broken", help="найти битые файлы (только отчёт)")
    brk.add_argument("--limit", type=int, default=0, help="остановиться после N находок")
    conv = sub.add_parser("convert", help="конвертировать .doc/.rtf/.odt в .docx")
    conv.add_argument("--apply", action="store_true", help="выполнить конвертацию (без флага — только список)")
    conv.add_argument("--limit", type=int, default=50)
    rep = sub.add_parser("repair-names", help="починить имена с битой кодировкой")
    rep.add_argument("--apply", action="store_true", help="переименовать (без флага — только предложения)")
    sp = sub.add_parser("structure", help="аудит дерева, план папок и (по запросу) их создание")
    sp.add_argument("--create", action="store_true", help="показать, какие папки из плана будут созданы")
    sp.add_argument("--apply", action="store_true", help="создать папки из structure_plan.txt; удалений нет")
    sp.add_argument("--years", nargs="*", default=[], help="годы для подпапок: --years 2024 2025 2026")

    ctx = sub.add_parser("context", help="показать промпт для одного файла")
    ctx.add_argument("--path", required=True, help="относительный путь из снимка")

    sug = sub.add_parser("suggest", help="разобрать партию файлов моделью")
    sug.add_argument("--limit", type=int, default=0)
    sug.add_argument("--prefix", default=None)
    sug.add_argument("--order", default="", choices=["", "path", "size", "modified", "shallow"])
    sug.add_argument("--media", action="store_true", help="включить фотографии")
    sug.add_argument("--vision", action="store_true", help="картинки и сканы читать зрением модели")
    sug.add_argument("--redo", action="store_true", help="не пропускать уже разобранные")
    sug.add_argument("--merge", action="store_true", help="сразу обновить карту")

    fld = sub.add_parser("folder", help="«папка целиком»: модель видит все имена папки сразу")
    fld.add_argument("--path", required=True, help="относительный путь папки от корня отдела")
    fld.add_argument("--limit", type=int, default=80)
    fld.add_argument("--vision", action="store_true", help="картинки прогнать через зрение модели")

    les = sub.add_parser("lessons", help="вынести уроки из правок и отказов в learned_rules.md")
    ref = sub.add_parser("refine", help="глубокая перепроверка одного файла (больше контекста)")
    ref.add_argument("--path", required=True)
    ref.add_argument("--comment", default="")

    app = sub.add_parser("apply", help="выполнить отмеченное в карте")
    app.add_argument("--word", default="", help="подтверждающее слово APPLY")
    und = sub.add_parser("undo", help="откатить последнюю пачку переносов")
    und.add_argument("--word", default="", help="подтверждающее слово UNDO")

    setg = sub.add_parser("settings", help="изменить настройку: settings llm.num_ctx=8192")
    setg.add_argument("pairs", nargs="*")
    return parser


COMMANDS = {
    "doctor": cmd_doctor,
    "scan": cmd_scan,
    "duplicates": cmd_duplicates,
    "models": cmd_models,
    "merge": cmd_merge,
    "dry-run": cmd_dry_run,
}


def ensure_single_instance() -> bool:
    """Два окна двигают одни и те же файлы и пишут одну карту — второй запуск блокируем."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ArhivariusMO")
        if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            return False
        globals()["_SINGLE_INSTANCE_MUTEX"] = handle       # держим до выхода процесса
        return True
    except Exception:
        return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    command = args.command or "gui"
    try:
        cfg_file = Path(args.config).resolve()
        config = load_config(cfg_file.parent, config_name=cfg_file.name)
    except Exception as exc:
        say(f"не загружен {args.config}: {type(exc).__name__}: {exc}")
        say("почините файл настроек или переименуйте его — создастся новый по умолчанию")
        return 2
    if command in ("gui", "apply", "undo") and not ensure_single_instance():
        msg = ("Архивариус уже запущен. Два экземпляра могут перемещать одни и те же файлы — "
               "закройте первый и повторите.")
        say(msg)
        if getattr(sys, "frozen", False):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "Архивариус", 0x30 | 0x1000)
            except Exception:
                pass
        return 1
    try:
        if command in COMMANDS:
            return COMMANDS[command](config)
        if command == "context":
            return cmd_context(config, args.path)
        if command == "suggest":
            return cmd_suggest(config, args)
        if command == "apply":
            return cmd_apply(config, args.word)
        if command == "undo":
            return cmd_undo(config, args.word)
        if command == "settings":
            return cmd_settings(config, args.pairs)
        if command == "plugins":
            return cmd_plugins(config)
        if command == "plugin-check":
            return cmd_plugin_check(config, args.plugin_id)
        if command == "plugin-install":
            return cmd_plugin_install(config, args.plugin)
        if command == "broken":
            return cmd_broken(config, int(args.limit))
        if command == "convert":
            return cmd_convert(config, bool(args.apply), int(args.limit))
        if command == "repair-names":
            return cmd_repair_names(config, bool(args.apply))
        if command == "structure":
            return cmd_structure(config, args)
        if command == "folder":
            return cmd_folder(config, args)
        if command == "lessons":
            return cmd_lessons(config, args)
        if command == "refine":
            return cmd_refine(config, args)
        return cmd_gui(config)
    except KeyboardInterrupt:
        say("\nпрервано оператором")
        return 130
    except SystemExit as exc:
        say(str(exc.code or ""))
        return 1
    except Exception as exc:
        say(f"\nошибка: {type(exc).__name__}: {exc}")
        if args.verbose:
            raise
        say("подробности: тот же запуск с -v")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

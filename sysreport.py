#!/usr/bin/env python3
"""Отчёт о компьютере и Ollama: что установлено, что потянет, как быстро работает.

    python sysreport.py            быстрый отчёт (железо, Ollama, установленные модели)
    python sysreport.py --bench    плюс замер скорости каждой модели и проверка чтения картинок
    python sysreport.py --url http://192.168.1.5:11434   если Ollama на другом компьютере

Только стандартная библиотека. В отчёт НЕ попадают имя пользователя, имя компьютера,
пути, адреса и серийные номера. Результат печатается и сохраняется в ``sysreport.txt``
рядом со скриптом — этот файл можно целиком отправить для подбора моделей.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

GB = 1024 ** 3
DEFAULT_URL = "http://127.0.0.1:11434"
REPORT_NAME = "sysreport.txt"
ENV_VALUE_HIDDEN = {"OLLAMA_HOST", "OLLAMA_MODELS", "OLLAMA_ORIGINS", "OLLAMA_TMPDIR"}


# ----------------------------------------------------------------- мелочи
def gb(value: float | int | None) -> str:
    return f"{(value or 0) / GB:.1f} ГБ"


def run(cmd: list[str], timeout: int = 15) -> str:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
        return done.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def clean_base(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return DEFAULT_URL
    if "://" not in value:
        value = "http://" + value
    if re.match(r"^https?://[^/:]+$", value) and value.startswith("http://"):
        value += ":11434"
    return value


# ----------------------------------------------------------------- железо
def parse_meminfo(text: str) -> tuple[int, int]:
    """/proc/meminfo → (всего, доступно) в байтах."""
    values = {m.group(1): int(m.group(2)) * 1024 for m in re.finditer(r"^(\w+):\s+(\d+)", text, re.M)}
    total = values.get("MemTotal", 0)
    free = values.get("MemAvailable") or (values.get("MemFree", 0) + values.get("Cached", 0))
    return total, free


def memory() -> tuple[int, int]:
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                        ("pagetotal", ctypes.c_ulonglong), ("pageavail", ctypes.c_ulonglong),
                        ("virttotal", ctypes.c_ulonglong), ("virtavail", ctypes.c_ulonglong),
                        ("ext", ctypes.c_ulonglong)]
        status = Status()
        status.length = ctypes.sizeof(Status)
        try:
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.total), int(status.avail)
        except Exception:
            return 0, 0
    if sys.platform == "darwin":
        total = int(run(["sysctl", "-n", "hw.memsize"]) or 0)
        return total, 0
    try:
        return parse_meminfo(Path("/proc/meminfo").read_text("utf-8", "replace"))
    except Exception:
        return 0, 0


def cpu_name() -> str:
    name = ""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
        except Exception:
            pass
    elif sys.platform == "darwin":
        name = run(["sysctl", "-n", "machdep.cpu.brand_string"])
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text("utf-8", "replace").splitlines():
                if line.lower().startswith("model name"):
                    name = line.split(":", 1)[1]
                    break
        except Exception:
            pass
    return re.sub(r"\s+", " ", name or platform.processor() or platform.machine()).strip()


def parse_nvidia_smi(text: str) -> list[dict]:
    """csv без заголовков: name, memory.total (МиБ), memory.used (МиБ), driver."""
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not re.match(r"^\d+$", parts[1]):
            continue
        gpus.append({"name": parts[0], "vram": int(parts[1]) * 1024 ** 2,
                     "used": int(parts[2]) * 1024 ** 2 if len(parts) > 2 and parts[2].isdigit() else 0,
                     "driver": parts[3] if len(parts) > 3 else "", "vendor": "NVIDIA"})
    return gpus


def vendor_of(name: str) -> str:
    low = name.lower()
    for key, vendor in (("nvidia", "NVIDIA"), ("geforce", "NVIDIA"), ("quadro", "NVIDIA"), ("rtx", "NVIDIA"),
                        ("amd", "AMD"), ("radeon", "AMD"), ("intel", "Intel"), ("apple", "Apple")):
        if key in low:
            return vendor
    return ""


def windows_gpus() -> list[dict]:
    """Все видеоадаптеры из реестра; там память хранится 64-битной (WMI обрезает на 4 ГБ)."""
    found = []
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for index in range(32):
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        name = winreg.QueryValueEx(key, "DriverDesc")[0]
                        try:
                            raw = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0]
                            vram = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
                        except OSError:
                            vram = 0
                    found.append({"name": name, "vram": vram, "used": 0, "driver": "", "vendor": vendor_of(name)})
                except OSError:
                    continue
    except Exception:
        pass
    if not found:
        raw = run(["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json"])
        try:
            data = json.loads(raw or "[]")
            for item in data if isinstance(data, list) else [data]:
                found.append({"name": item.get("Name", ""), "vram": int(item.get("AdapterRAM") or 0),
                              "used": 0, "driver": "", "vendor": vendor_of(item.get("Name", ""))})
        except Exception:
            pass
    return found


def gpus(total_ram: int) -> list[dict]:
    smi = shutil.which("nvidia-smi")
    found = parse_nvidia_smi(run([smi, "--query-gpu=name,memory.total,memory.used,driver_version",
                                  "--format=csv,noheader,nounits"])) if smi else []
    if found:
        return found
    if sys.platform == "win32":
        return windows_gpus()
    if sys.platform == "darwin":
        chip = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if "apple" in chip.lower():
            return [{"name": chip, "vram": total_ram, "used": 0, "driver": "", "vendor": "Apple",
                     "unified": True}]
    out = run(["lspci"])
    return [{"name": m.group(1).strip(), "vram": 0, "used": 0, "driver": "", "vendor": vendor_of(m.group(1))}
            for m in re.finditer(r"(?:VGA compatible|3D) controller:\s*(.+)", out)]


def models_dir() -> Path:
    custom = os.environ.get("OLLAMA_MODELS")
    return Path(custom) if custom else Path.home() / ".ollama" / "models"


def disk_free() -> tuple[int, int, bool]:
    folder = models_dir()
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
        return usage.free, usage.total, bool(os.environ.get("OLLAMA_MODELS"))
    except Exception:
        return 0, 0, bool(os.environ.get("OLLAMA_MODELS"))


def model_budget(gpu_list: list[dict], ram_total: int) -> tuple[int, str]:
    """Сколько памяти реально доступно модели и откуда она берётся."""
    best = max(gpu_list, key=lambda g: g.get("vram", 0), default=None)
    if best and best.get("unified"):
        return int(ram_total * 0.65), "единая память Apple (около 65% от всей)"
    if best and best.get("vram", 0) >= 3 * GB and best.get("vendor") in ("NVIDIA", "AMD"):
        return int(best["vram"]), f"видеопамять {best['name']}"
    return int(ram_total * 0.55), "оперативная память (видеокарта не подходит или не найдена) — будет медленнее"


def size_tiers(budget: int) -> str:
    """Какие размеры моделей (веса в Q4 + запас на контекст ~2-3 ГБ) умещаются целиком."""
    room = budget / GB - 2.5
    tiers = [(4, "до 4B"), (8, "до 8B"), (13, "до 14B"), (20, "до 27B"), (40, "до 32B+")]
    fit = "ничего крупнее 1-2B"
    for need, label in tiers:
        if room >= need * 0.62 + 0.5:
            fit = label
    return fit


# ----------------------------------------------------------------- Ollama
class Api:
    def __init__(self, base: str, timeout: int = 20):
        self.base = clean_base(base)
        self.timeout = timeout
        host = re.sub(r"^https?://", "", self.base).split(":")[0].split("/")[0]
        local = host in ("localhost", "127.0.0.1", "::1") or re.match(r"^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.", host)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local \
            else urllib.request.build_opener()

    def call(self, path: str, body: dict | None = None, timeout: int | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            try:
                detail = json.loads(detail).get("error", detail)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        except Exception as exc:
            raise RuntimeError(str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)) from None


def context_length(info: dict) -> int:
    for key, value in (info.get("model_info") or {}).items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return 0


def inspect_models(api: Api) -> list[dict]:
    result = []
    for item in api.call("/api/tags").get("models", []):
        name = item.get("name") or item.get("model") or ""
        details = item.get("details") or {}
        row = {"name": name, "size": int(item.get("size") or 0), "params": details.get("parameter_size", ""),
               "quant": details.get("quantization_level", ""), "family": details.get("family", ""),
               "caps": [], "ctx": 0}
        try:
            info = api.call("/api/show", {"model": name})
            row["caps"] = list(info.get("capabilities") or [])
            row["ctx"] = context_length(info)
            shown = info.get("details") or {}
            row["params"] = row["params"] or shown.get("parameter_size", "")
            row["quant"] = row["quant"] or shown.get("quantization_level", "")
        except RuntimeError as exc:
            row["error"] = str(exc)
        result.append(row)
    return result


def is_embedding(row: dict) -> bool:
    return "embedding" in row["caps"] and "completion" not in row["caps"] or \
        any(m in row["name"].lower() for m in ("embed", "bge-", "minilm"))


def make_png(width: int = 224, height: int = 224) -> bytes:
    """PNG: верхняя половина красная, нижняя синяя — без сторонних библиотек."""
    rows = b"".join(b"\x00" + (b"\xe0\x20\x20" if y < height // 2 else b"\x20\x20\xe0") * width
                    for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def chat(api: Api, model: str, text: str, caps: list[str], image: bytes | None = None, timeout: int = 600) -> dict:
    message = {"role": "user", "content": text}
    if image:
        message["images"] = [base64.b64encode(image).decode()]
    body = {"model": model, "messages": [message], "stream": False, "keep_alive": "2m",
            "options": {"num_ctx": 4096, "num_predict": 64, "temperature": 0}}
    if "thinking" in caps:
        body["think"] = False
    return api.call("/api/chat", body, timeout=timeout)


def speed(reply: dict) -> dict:
    tokens = int(reply.get("eval_count") or 0)
    seconds = float(reply.get("eval_duration") or 0) / 1e9
    prompt_s = float(reply.get("prompt_eval_duration") or 0) / 1e9
    return {"tps": tokens / seconds if seconds > 0 else 0.0, "tokens": tokens,
            "load": float(reply.get("load_duration") or 0) / 1e9,
            "prompt_tps": int(reply.get("prompt_eval_count") or 0) / prompt_s if prompt_s > 0 else 0.0,
            "total": float(reply.get("total_duration") or 0) / 1e9,
            "text": (reply.get("message") or {}).get("content", "").strip()}


def gpu_share(api: Api, model: str) -> str:
    try:
        for item in api.call("/api/ps").get("models", []):
            if item.get("name") == model or item.get("model") == model:
                size, vram = int(item.get("size") or 0), int(item.get("size_vram") or 0)
                if size:
                    return f"{round(vram * 100 / size)}% на видеокарте ({gb(size)} в памяти)"
    except RuntimeError:
        pass
    return "?"


def unload(api: Api, model: str) -> None:
    try:
        api.call("/api/chat", {"model": model, "keep_alive": 0, "messages": []}, timeout=30)
    except RuntimeError:
        pass


def bench(api: Api, rows: list[dict], limit: int, lines: list[str]) -> None:
    png = make_png()
    chosen = [r for r in rows if not is_embedding(r)]
    chosen.sort(key=lambda r: r["size"])
    for row in chosen[:limit]:
        name = row["name"]
        print(f"   замер {name} ...", flush=True)
        try:
            first = speed(chat(api, name, "Назови три цвета радуги через запятую.", row["caps"]))
            second = speed(chat(api, name, "Перечисли пять фруктов через запятую.", row["caps"]))
            lines.append(f"  {name}: {second['tps']:.1f} ток/с (первый запуск {first['load']:.1f} с на загрузку), "
                         f"разбор промпта {second['prompt_tps']:.0f} ток/с, {gpu_share(api, name)}")
            if "vision" in row["caps"]:
                t0 = time.time()
                seen = speed(chat(api, name, "Назови два цвета на картинке (верхний и нижний). Коротко.",
                                  row["caps"], image=png))["text"].lower()
                ok = any(w in seen for w in ("красн", "red")) and any(w in seen for w in ("син", "голуб", "blue"))
                lines.append(f"     картинка: {'ЧИТАЕТ' if ok else 'ответ не совпал'} за {time.time() - t0:.1f} с "
                             f"→ «{seen[:80]}»")
        except RuntimeError as exc:
            lines.append(f"  {name}: ОШИБКА — {str(exc)[:160]}")
        finally:
            unload(api, name)


# ----------------------------------------------------------------- отчёт
def build_report(url: str, do_bench: bool, bench_limit: int) -> list[str]:
    lines: list[str] = []
    add = lines.append
    add("ОТЧЁТ О КОМПЬЮТЕРЕ И OLLAMA")
    add("=" * 26)
    add(f"Система:     {platform.system()} {platform.release()} ({platform.machine()})")
    add(f"Python:      {sys.version.split()[0]}")
    add(f"Процессор:   {cpu_name()} · потоков {os.cpu_count()}")
    total, avail = memory()
    add(f"Память ОЗУ:  {gb(total)}" + (f", свободно сейчас {gb(avail)}" if avail else ""))
    gpu_list = gpus(total)
    if gpu_list:
        for g in gpu_list:
            extra = f" · драйвер {g['driver']}" if g.get("driver") else ""
            used = f", занято {gb(g['used'])}" if g.get("used") else ""
            vram = gb(g["vram"]) if g.get("vram") else "объём не определён"
            kind = "единая память" if g.get("unified") else "видеопамять"
            add(f"Видеокарта:  {g['name']} · {kind} {vram}{used}{extra}")
    else:
        add("Видеокарта:  не найдена (работа на процессоре)")
    free, size, custom = disk_free()
    add(f"Диск моделей: свободно {gb(free)} из {gb(size)}" + (" (папка моделей переопределена)" if custom else ""))
    budget, source = model_budget(gpu_list, total)
    add(f"Бюджет под модель: ~{gb(budget)} — {source}")
    add(f"Целиком в память помещаются модели {size_tiers(budget)} (с запасом под контекст)")

    add("\nПЕРЕМЕННЫЕ OLLAMA")
    envs = sorted(k for k in os.environ if k.startswith("OLLAMA_"))
    if envs:
        for key in envs:
            add(f"  {key} = {'(задано)' if key in ENV_VALUE_HIDDEN else os.environ[key]}")
    else:
        add("  не заданы (значения по умолчанию)")

    add("\nOLLAMA")
    api = Api(url)
    try:
        version = api.call("/api/version").get("version", "?")
    except RuntimeError as exc:
        add(f"  НЕ ОТВЕЧАЕТ: {exc}")
        add("  Запустите приложение Ollama (значок в трее) и повторите. "
            + ("Программа ollama найдена." if shutil.which("ollama") else "Команда ollama не найдена в PATH."))
        return lines
    add(f"  версия {version}")
    try:
        rows = inspect_models(api)
    except RuntimeError as exc:
        add(f"  не удалось получить список моделей: {exc}")
        return lines
    add(f"  установлено моделей: {len(rows)}")
    for row in sorted(rows, key=lambda r: r["size"], reverse=True):
        caps = ",".join(c for c in row["caps"]) or "?"
        ctx = f", контекст до {row['ctx']}" if row["ctx"] else ""
        extra = f" · ОШИБКА {row['error']}" if row.get("error") else ""
        add(f"   - {row['name']}: {gb(row['size'])}, {row['params'] or '?'}, {row['quant'] or '?'}, "
            f"возможности [{caps}]{ctx}{extra}")
    try:
        running = api.call("/api/ps").get("models", [])
        if running:
            add("  сейчас в памяти: " + "; ".join(
                f"{m.get('name')} {gb(m.get('size'))}" for m in running))
    except RuntimeError:
        pass

    if do_bench:
        add("\nЗАМЕР СКОРОСТИ (контекст 4096, температура 0)")
        bench(api, rows, bench_limit, lines)
    else:
        add("\nСкорость не измерялась. Для замера: python sysreport.py --bench")
    return lines


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Отчёт о компьютере и Ollama")
    parser.add_argument("--url", default=os.environ.get("OLLAMA_HOST", DEFAULT_URL), help="адрес Ollama")
    parser.add_argument("--bench", action="store_true", help="замерить скорость и проверку картинок")
    parser.add_argument("--max-models", type=int, default=6, help="сколько моделей мерить (самые маленькие первыми)")
    parser.add_argument("--out", default=str(Path(__file__).resolve().with_name(REPORT_NAME)))
    args = parser.parse_args(argv)

    lines = build_report(args.url, args.bench, max(1, args.max_models))
    text = "\n".join(lines) + "\n"
    print(text)
    try:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Сохранено: {args.out}\nОтправьте мне этот текст (или файл) целиком.")
    except OSError as exc:
        print(f"Файл не сохранён ({exc}); скопируйте текст выше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

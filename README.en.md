# Архивариус (File Archivist)

![logo](logo.png)

**An offline folder-archiving assistant powered by a local LLM.** It reads the *content*
of your files (documents, PDFs, spreadsheets, images — via the model's vision), proposes
a name following your own naming convention and a destination inside your folder tree,
and moves only what a human approves. No clouds, no API keys: files, maps and decisions
stay on your computer.

Other languages: [Русский](README.md) · [Download .exe](https://github.com/Feoodor/arhivarius/releases/latest)

## What it is for

Folders grow junk over the years: "To sort", "New folder (2)", `IMG_2041.jpg`, documents
in the wrong place with the wrong names. Sorting by hand takes weeks. Архивариус does the
grunt work: the model reads each file and proposes a name and a destination; you approve.
Everything else is a safety net: an operation journal, one-click batch undo, zero deletes.

## Features

| | |
|---|---|
| 🧠 **Two-pass analysis** | pass 1 picks the section using the full folder map, pass 2 the exact folder inside its subtree — any nesting depth is covered |
| ⚡ **Rules before model** | obvious cases (canonical name already, filename names an existing folder, repeated sha256, shortcuts) resolve deterministically without an LLM call |
| 📖 **Few-shot precedents** | your approved decisions are injected into prompts as examples — the system learns from confirmations, no fine-tuning |
| 👁 **Vision** | images and PDF scans are read visually (gemma3): type, topic, date — even text printed on a poster |
| 🗣 **Extraction & OCR** | docx/pdf/xlsx/pptx/rtf, photo/audio metadata, tesseract OCR |
| 🧭 **Folder navigator** | a separate pass proposes where each "to sort" folder belongs, with per-folder stats (text vs images) and a time estimate |
| 📂 **Folders window** | empty folders → Windows Recycle Bin (with "keep" marks), same-name folder merge into the canonical one, missing-folder taxonomy hints |
| 🖼 **Similar photos** | perceptual dhash finds re-compressed/renamed copies of the same shot |
| 🔎 **Embeddings (optional)** | semantic folder retrieval with a local embedding model (bge-m3) |
| 🛡 **Guardrails** | dates in names must be backed by text/EXIF; invented folders cap confidence; target outside the chosen section → review; name clash → `_v2` + review |
| 🗂 **Suggestion map** | Excel-friendly CSV (`;` + BOM), filters, a "was/will be" reviewer with file content side by side |
| ↩️ **Journal & undo** | every applied batch rolls back with one button; files are never deleted |
| 🌍 **10 UI languages** | English, Русский, Беларуская, Українська, Polski, Deutsch, Español, Français, Italiano, Português, 中文 |
| 🔌 **LAN** | inference can run against Ollama on another machine in your local network |

## Quick start

1. Install [Ollama](https://ollama.com) and pull a model:
   `ollama pull gemma3:12b` (optionally `ollama pull bge-m3` for semantic folder search).
2. Download `Arhivarius-*.exe` from the [latest release](https://github.com/Feoodor/arhivarius/releases)
   and run it. No Python required.
3. The first run creates `config.json` and editable prompts (`prompt.md` — your naming
   rules and layout policy; plus `prompt_user.md`, `prompt_section.md`, `prompt_folder.md`).
   Set the archive root and confidence thresholds in ⚙️ Settings.
4. Workflow: Refresh snapshot → Run a batch (5–20 files) → Review & approve →
   Dry-run move → Apply approved.

## Data safety

- Fully **local**: file contents only ever reach your own Ollama instance. Nothing goes to the cloud.
- Files are **never deleted or overwritten**: "trash" is a move into a separate folder,
  empty folders go to the Windows Recycle Bin — all journaled and reversible.
- Windows `.lnk` shortcuts created for letters are not cloud links; Google Docs shortcuts
  (`.gdoc`, `.gsheet`, …) are zero-byte by design and are never treated as junk.

## Building from source

```
pip install -r requirements.txt      # PyMuPDF, openpyxl, Pillow (optional readers)
python app.py                        # GUI
python selftest.py                   # 100+ automated tests on a temp tree
python etalon_check.py               # accuracy on golden cases (needs Ollama)
pyinstaller Архивариус.spec          # single .exe
```

## Ideas welcome

This project improves fastest through user ideas — open an
[Issue](https://github.com/Feoodor/arhivarius/issues) or start a
[Discussion](https://github.com/Feoodor/arhivarius/discussions): a new file format,
a UI language, a naming rule, model behaviour. Bugs (with a log) are especially
appreciated. PRs welcome.

Roadmap: scheduled batch mode, before/after diff for ambiguous folders, usage
statistics export, selectable embedding models.

## License

AGPL-3.0 — use, study, modify and share, keeping modifications open.

---

## Contact

Developed by **Feodor Moskalchuk**. Questions and ideas:
Telegram **[@Feo_odor](https://t.me/Feo_odor)** · GitHub Issues · Discussions.


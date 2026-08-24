# O'Reilly Ingest

We're in the AI era. You want to chat with your favorite technical books using Claude Code, Cursor, or any LLM tool. This gets you there.

Export any O'Reilly book to Markdown, PDF, EPUB, JSON, TOON, or plain text. Download by chapters so you don't burn through your context window.

> Requires a valid O'Reilly Learning subscription.

## Disclaimer

For personal and educational use only. Please read the [O'Reilly Terms of Service](https://www.oreilly.com/terms/).

## Credits

Fork of [oreilly-ingest](https://github.com/Mosaibah/oreilly-ingest) by [@Mosaibah](https://github.com/Mosaibah).

Inspired by [safaribooks](https://github.com/lorenzodifuccia/safaribooks) by [@lorenzodifuccia](https://github.com/lorenzodifuccia).


## Features

- **Export by chapters** - save tokens, focus on what matters
- **LLM-ready formats** - Markdown, JSON, TOON, plain text optimized for AI
- **Traditional formats** - PDF and EPUB 3
- **O'Reilly V2 API** - fast and reliable
- **Images & styles included** - complete book experience
- **Web UI** - search, preview, download
- **Translation with a local LLM** *(beta)* - translate while downloading, via Ollama

<img src="docs/main.png" alt="Main Page">

## Quick Start

### Docker

```bash
git clone https://github.com/hannahNchan/oreilly-downloader.git
cd oreilly-downloader
docker compose up -d
```

### Python

```bash
git clone https://github.com/hannahNchan/oreilly-downloader.git
cd oreilly-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open http://localhost:8000

## Setting Up Cookies

Click "Set Cookies" in the web interface and follow the steps:

<img src="docs/cookie-modal.png" alt="Cookie Setup" style="max-width:320px; height:auto;">

## Translation (local LLM)

> **BETA.** It works, but expect rough edges: a full book takes hours, the
> output is not proofread, and quality depends heavily on the model you pick.
> Treat the result as a draft, not a finished translation.

Chapters are translated by a model running on your own machine, as part of the
download. Nothing is sent to a third-party service and no API key is involved.

### Setup

Install [Ollama](https://ollama.com), pull a model, and leave it running:

```bash
ollama pull qwen3.5:9b
```

Then pick a language under **Translate (local LLM)** in the download modal.
`Original (no translation)` is the default, so nothing changes until you ask for
it. If Ollama isn't reachable the download fails immediately instead of quietly
giving you an untranslated book.

### Choosing a model

A model that fits entirely in VRAM beats a bigger one that spills to CPU. On the
same chapter, `qwen3.5:9b` (~6.6 GB) measured roughly **3x faster** than
`qwen3-coder:30b` (18.6 GB, offloaded) — and coder-tuned models are worse at
prose than general ones. Very small models are a false economy: sub-7B ones
mangled short fragments, returning a whole paragraph where the source had two
words.

### Configuration

All of it lives in `config.py`:

| Setting | Default | What it does |
|---------|---------|--------------|
| `OLLAMA_URL` | `http://localhost:11434` | Where Ollama listens |
| `OLLAMA_MODEL` | `qwen3.5:9b` | Model used to translate |
| `OLLAMA_NUM_CTX` | `16384` | Context window. **Don't lower this** — Ollama's 4096 default silently truncates the reply, and whole passages come back untranslated |
| `OLLAMA_DISABLE_THINKING` | `True` | Skips the "reasoning" pass, which is wasted time here. Models that reject the flag are retried without it |
| `TRANSLATE_BATCH_CHARS` | `4000` | Characters per request. Fewer round-trips is much faster |
| `TRANSLATE_TIMEOUT` | `300` | Seconds allowed per request |
| `TRANSLATE_LANGUAGES` | `es-LATAM` | Target languages, as `code -> instruction for the model`. Add your own here |

### What it will and won't touch

- **Code is never translated.** `pre`, `code`, `kbd`, `samp`, `var`, `tt`,
  `script` and `style` are left exactly as they came.
- Text is translated **per block** — paragraph, list item, heading, table cell —
  not per text node. A sentence broken up by `<em>` or `<code>` reaches the model
  as one sentence, which is the difference between a translation and word salad.
- Every result is validated before being accepted: if the returned markup lost a
  tag or dropped the contents of a `<code>`, the original block is kept.
- Batches that come back with entries missing are retried in halves, so one bad
  response doesn't cost you a chapter.

### Known limits

- **Slow.** Hours for a full book on consumer hardware. The download itself takes
  minutes; the translation is everything after that.
- **Your O'Reilly session may be shorter than the job.** Chapters are all fetched
  first and translated afterwards, so the session only has to survive the
  download phase. If it does expire mid-download you get an explicit error, not a
  silently truncated book — paste fresh cookies and run it again.
- Only `es-LATAM` (neutral Latin American Spanish) ships configured.

## Architecture

Plugin-based microkernel design:

| Layer | Components |
|-------|------------|
| **Kernel** | Plugin registry, shared HTTP client |
| **Core** | Auth, Book, Chapters, Assets, HtmlProcessor |
| **Output** | Epub, Markdown, Pdf, PlainText, JsonExport, ToonExport |
| **Utility** | Chunking, Token, Downloader |

### API

```
GET  /api/status       - auth check
GET  /api/search?q=    - find books
GET  /api/book/{id}    - metadata
POST /api/download     - start export
GET  /api/progress     - SSE stream
```

## Contributing

Found a bug or have an idea? PRs and issues are always welcome!


## Recent Changes

- **Chunking: streaming & memory fix** — `chunk_book()` now streams chunks directly to disk instead of accumulating in memory. Replaced `tiktoken` tokenizer with a word-count heuristic to avoid memory spikes on large books. (@zirkleta)
- **System: command injection fix** — `_show_macos_picker()` rejects paths containing `"` before interpolating into osascript, preventing command injection via crafted directory names. (@zirkleta)
- **`patch_chunk_titles.py`** — New utility script that backfills `book_title` into existing `*_chunks.jsonl` files in the output directory. (@zirkleta)

## License

MIT

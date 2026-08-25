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
- **Audiobooks** - download tracks and play them in the browser
- **Download queue** - queue many books, one at a time, pause and resume
- **Built-in library** - read your EPUBs in the browser, no external reader
- **"Para después" list** - save titles now, download them later
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

Open the console on a logged-in `learning.oreilly.com` tab, run
`copy(document.cookie)`, and paste. Two formats are accepted, so it doesn't
matter how you copied them:

```
orm-jwt=eyJ...; _abck=...; bm_sz=...      <- the raw document.cookie string
{"orm-jwt": "eyJ...", "_abck": "..."}     <- or JSON
```

`orm-jwt` is the session cookie and is required. Without it the paste is
rejected up front, instead of being saved as a set that fails on the first
download.

O'Reilly rotates that token every few minutes and there is no way to renew it
from here, so expect to paste again during long jobs. That is what the queue's
pause is for.

## Downloads

Downloads land in `output/` first and are published to the library afterwards.
Writing straight into a remote folder stretched the network phase — thousands of
small writes with per-file latency — exactly when the O'Reilly token has minutes
to live.

### The queue

One download at a time, in order. Deliberate, not a simplification: the HTTP
client is shared and resets its cookie jar per request, so two downloads at once
would step on each other, and raising the request rate against O'Reilly is what
triggers the blocks.

- **Select many at once.** With search results on screen, hit **Múltiples**, tick
  the books you want, and open the batch modal. Each entry expands for its own
  format, translation and *Omitir imágenes* — per book, not global.
- **Close the modal and it keeps going.** A floating pill shows how many are
  active and reopens the modal with live progress. It shows the current downloads
  only, not the history of the session.
- **Session expiry pauses instead of failing.** The job stops at `paused`, the
  queue holds, and the cookie form appears *inside* the downloads modal. Paste,
  press the button, and it continues from where it was.
- **Resuming is cheap.** Fetched chapters are cached per book and audio tracks
  already on disk are skipped, so a retry doesn't re-download what you have.
- **Only one server downloads.** Two servers pointing at the same `data/` would
  work the same queue. The first to take an OS-level lock on `data/queue.lock`
  runs it; the other serves and displays the queue but executes nothing, and says
  so in the UI. It takes over on its own if the first one closes.

### Mis libros

The **Mis libros** menu holds two sections:

- **Biblioteca** — everything on disk. EPUBs open in a built-in reader (epub.js,
  vendored, no CDN); audiobooks get a player with chapter names, and incomplete
  downloads are flagged.
- **Para después** — titles saved for later, in `data/watchlist.json`. Anything
  already downloaded is marked as such, and **Limpiar descargados** clears those
  from the list in one go. It asks first, and it never touches the files.

### Where the library lives

**Settings** (gear icon) sets the library folder. Unset, it defaults to
`output/library`: the app builds the structure there and nothing else is needed.
Point it anywhere you like, including a network share — downloads still go
through the local cache first and are transferred on completion, verified byte
for byte.

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
| **Core** | Auth, Book, Chapters, Assets, HtmlProcessor, Audiobook |
| **Output** | Epub, Markdown, Pdf, PlainText, JsonExport, ToonExport |
| **Storage** | Output (local cache), Library (published works), Watchlist |
| **Utility** | Chunking, Token, Translator, Downloader, Queue, System |

The library is content-addressed: `work_id = sha1("urn:orm:{kind}:{book_id}")[:8]`,
sharded by its first two characters, so `index/library.json` can be rebuilt from
disk at any time. Download state is derived, never stored — a work counts as
downloaded because the files are there, not because a flag says so.

### API

```
GET  /api/status                      - auth check
GET  /api/search?q=                   - find books
GET  /api/book/{id}                   - metadata
GET  /api/audiobook/{id}              - audiobook metadata
POST /api/download                    - queue an export
POST /api/cookies                     - store a fresh session
GET  /api/progress                    - SSE stream

GET  /api/queue                       - jobs, active, paused, owner
POST /api/queue/{job}/cancel          - drop one job
POST /api/queue/clear                 - forget finished jobs

GET  /api/library                     - works on disk
GET  /api/library/file/{w}/(epub|pdf) - stream a file (Range supported)
GET  /api/library/audio/{w}/{n}       - stream a track (Range supported)
POST /api/library/transfer            - publish from cache to the library

GET  /api/watchlist                   - "Para después", with download state
POST /api/watchlist                   - save a title
POST /api/watchlist/{id}/remove       - drop one
POST /api/watchlist/clear-downloaded  - drop everything already downloaded

GET  /api/settings                    - current preferences
POST /api/settings/library-dir        - set the library folder
```

## Contributing

Found a bug or have an idea? PRs and issues are always welcome!


## Recent Changes

- **Sequential download queue** — many books queued, one at a time. Session
  expiry pauses the queue instead of losing the job; pasting fresh cookies
  resumes it from where it was.
- **Multi-select downloads** — pick several search results and configure each one
  separately (format, translation, skip images) in a single modal.
- **"Para después" watchlist** — save titles before downloading them, under the
  new **Mis libros** menu, with a one-click clean-up of the ones already on disk.
- **Built-in library and reader** — content-addressed storage, a self-healing
  index rebuilt from disk, an in-browser EPUB reader, and an audiobook player with
  incomplete-download detection.
- **User-configurable library folder** — set in Settings, defaults to
  `output/library`. Downloads use the local cache first and are transferred and
  verified afterwards.
- **Session detection: fewer false alarms** — a short chapter ending in an
  ellipsis is no longer assumed to be a paywall preview. The session is verified
  against a chapter that already came back in full, so genuinely short chapters
  (conclusions, appendices) no longer stall a download that no cookie could fix.
- **Cookie input accepts both formats** — the raw `document.cookie` string or
  JSON, with `orm-jwt` required, and the result checked against the server
  instead of reporting success blindly.
- **One downloader per machine** — an OS-level lock decides which server runs the
  queue, released automatically when the process dies. Others display the queue
  and take over on their own if the owner closes.
- **Chunking: streaming & memory fix** — `chunk_book()` now streams chunks directly to disk instead of accumulating in memory. Replaced `tiktoken` tokenizer with a word-count heuristic to avoid memory spikes on large books. (@zirkleta)
- **System: command injection fix** — `_show_macos_picker()` rejects paths containing `"` before interpolating into osascript, preventing command injection via crafted directory names. (@zirkleta)
- **`patch_chunk_titles.py`** — New utility script that backfills `book_title` into existing `*_chunks.jsonl` files in the output directory. (@zirkleta)

## License

MIT

"""Service configuration. Every value is overridable by environment variable.

Defaults are tuned for the machine this runs on (RTX 3060, 12 GB, Windows) and
for the only language pair the service exists to serve: English -> Latin
American Spanish.

This module deliberately does NOT import the parent app's config.py. The
service is a separate process with its own venv; it must start even if the
oreilly-ingest app is not importable.
"""

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _s(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- Model -----------------------------------------------------------------
# Lives on D: because the C: drive is not an option on this machine. Outside the
# repository, so nothing here tracks or cleans it up: 3.4 GB deleted by hand
# when you want it gone.
MODEL_DIR = Path(_s("NLLB_MODEL_DIR", r"D:\ollama\models\nllb-200-3.3B-ct2-int8"))

DEVICE = _s("NLLB_DEVICE", "cuda")
# int8_float16: 8-bit weights, fp16 accumulation. ~3.6 GB resident on this GPU.
COMPUTE_TYPE = _s("NLLB_COMPUTE_TYPE", "int8_float16")
DEVICE_INDEX = _i("NLLB_DEVICE_INDEX", 0)

# On GPU, inter_threads > 1 allocates one CUDA stream *and its workspace* per
# thread, multiplying VRAM for no gain with a single client. Keep it at 1.
INTER_THREADS = _i("NLLB_INTER_THREADS", 1)
INTRA_THREADS = _i("NLLB_INTRA_THREADS", 0)  # 0 = let CTranslate2 decide

# --- Languages -------------------------------------------------------------
DEFAULT_SOURCE_LANG = _s("NLLB_SOURCE_LANG", "eng_Latn")
DEFAULT_TARGET_LANG = _s("NLLB_TARGET_LANG", "spa_Latn")

# --- Decoding --------------------------------------------------------------
BEAM_SIZE = _i("NLLB_BEAM_SIZE", 4)
MAX_BEAM_SIZE = _i("NLLB_MAX_BEAM_SIZE", 8)

# Batches are measured in TOKENS, not in sentences: 32 long sentences and 32
# short ones are completely different loads on the GPU, so a fixed sentence
# count is a time bomb. 2048 tokens is ~60-80 book sentences and leaves several
# GB of headroom for the Windows desktop, whose own VRAM use is not constant
# (a couple of Chrome tabs with video move it by 1-2 GB).
MAX_BATCH_TOKENS = _i("NLLB_MAX_BATCH_TOKENS", 2048)

# NLLB is a sentence-level model. Our splitter aims below the hard cap so that
# small disagreements between our token count and CTranslate2's can never reach
# max_input_length, where the input would be truncated silently.
SENTENCE_TOKEN_TARGET = _i("NLLB_SENTENCE_TOKEN_TARGET", 480)
MAX_INPUT_TOKENS = _i("NLLB_MAX_INPUT_TOKENS", 512)

# CTranslate2 defaults max_decoding_length to 256 tokens. Spanish runs 15-25%
# longer than English, so a long source sentence would come back cut off
# mid-phrase, with no error anywhere. This has to be raised, not left alone.
MAX_DECODING_TOKENS = _i("NLLB_MAX_DECODING_TOKENS", 1024)

# NLLB's known failure mode on noisy input is looping: it repeats a phrase until
# it hits the decoding limit. no_repeat_ngram_size=3 stops that, but it also
# forbids legitimate repetition ("uno, dos, tres, uno, dos, tres"), so it is off
# by default. Turn it on if you actually see looping.
NO_REPEAT_NGRAM_SIZE = _i("NLLB_NO_REPEAT_NGRAM_SIZE", 0)

# --- Request limits --------------------------------------------------------
MAX_TEXT_CHARS = _i("NLLB_MAX_TEXT_CHARS", 200_000)
MAX_BATCH_ITEMS = _i("NLLB_MAX_BATCH_ITEMS", 1000)

# Answers the client; does NOT cancel the GPU work already in flight (see the
# note in main.py). What actually bounds the work is MAX_TEXT_CHARS above.
REQUEST_TIMEOUT = _i("NLLB_REQUEST_TIMEOUT", 600)

# --- VRAM guards -----------------------------------------------------------
# Refuse to load if there is not enough free VRAM for the model plus a working
# margin: a clear message beats an unreadable CUDA error 20 seconds in.
VRAM_MIN_FREE_MB = _i("NLLB_VRAM_MIN_FREE_MB", 4608)
# /health reports "low" below this, so a climbing desktop baseline is visible
# before it turns into an OOM.
VRAM_WARN_FREE_MB = _i("NLLB_VRAM_WARN_FREE_MB", 1536)

# --- Post-edition ----------------------------------------------------------
# NLLB has exactly one Spanish (spa_Latn). There is no es-419 and no way to ask
# for neutral Latin American Spanish, because there is no prompt to ask with.
# This deterministic list fixes the handful of lexical items where the model's
# Spanish reads peninsular. See data/postedit_spa_Latn.txt.
POSTEDIT_ENABLED = _b("NLLB_POSTEDIT", True)
POSTEDIT_FILE = Path(_s("NLLB_POSTEDIT_FILE", str(ROOT / "data" / "postedit_spa_Latn.txt")))
POSTEDIT_LANGS = {"spa_Latn"}

# --- Server ----------------------------------------------------------------
# Loopback only: this service has no auth and is not meant to leave the machine.
HOST = _s("NLLB_HOST", "127.0.0.1")
PORT = _i("NLLB_PORT", 8100)

# Translate one short sentence at startup so the first real request does not pay
# for lazy CUDA kernel and cuBLAS handle initialisation.
WARMUP = _b("NLLB_WARMUP", True)

LOG_LEVEL = _s("NLLB_LOG_LEVEL", "INFO")

import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"

# Use data/ directory if it exists (Docker), otherwise use root (local dev)
DATA_DIR = BASE_DIR / "data"
if DATA_DIR.exists():
    COOKIES_FILE = DATA_DIR / "cookies.json"
else:
    COOKIES_FILE = BASE_DIR / "cookies.json"

BASE_URL = "https://learning.oreilly.com"
API_V1 = f"{BASE_URL}/api/v1"
API_V2 = f"{BASE_URL}/api/v2"

REQUEST_DELAY = 0.5
REQUEST_TIMEOUT = 30

# Retry transient network failures (timeouts, connection resets, Akamai
# throttling stalls) so a single bad response doesn't abort a whole download.
MAX_RETRIES = 4
RETRY_BACKOFF = 1.5

# --- Biblioteca ---
# Raiz donde viven las obras ya publicadas, con su propia estructura:
# objects/<shard>/<work_id>/, index/library.json y covers/. Por defecto queda
# dentro de output/, asi que la app funciona sin configurar nada, y el usuario
# puede apuntarla a la carpeta que quiera (otro disco, una unidad de red).
#
# La descarga NUNCA escribe aqui directamente: primero cae en OUTPUT_DIR y de
# ahi se pasa. Asi un corte a mitad de camino no deja media obra publicada.
DEFAULT_LIBRARY_DIR = OUTPUT_DIR / "library"
LIBRARY_DIR = DEFAULT_LIBRARY_DIR

# Los ajustes del usuario van en un archivo aparte (data/, ya ignorado por git)
# para que el repo se pueda compartir sin llevarse las rutas de nadie.
SETTINGS_FILE = (DATA_DIR if DATA_DIR.exists() else BASE_DIR) / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}  # sin ajustes guardados se usan los defaults


SETTINGS = _load_settings()
if SETTINGS.get("library_dir"):
    LIBRARY_DIR = Path(SETTINGS["library_dir"])


def save_setting(key: str, value) -> None:
    """Persiste un ajuste.

    Escribe a un temporal y renombra: si el proceso muere a mitad de escritura,
    el archivo de ajustes anterior sigue intacto en vez de quedar truncado.
    """
    SETTINGS[key] = value
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(SETTINGS, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(SETTINGS_FILE)

# --- Translation (Ollama) ---
# Local LLM used to translate chapter content. Ollama exposes a plain HTTP API
# with no auth, so the translator uses its own small client (not HttpClient,
# which is coupled to O'Reilly's cookies/Akamai handling).
OLLAMA_URL = "http://localhost:11434"
# A general-purpose model that fits entirely in VRAM beats a bigger one that
# spills to CPU: qwen3.5:9b (~6.6GB) measured ~3x faster than qwen3-coder:30b
# (18.6GB, offloaded) on the same chapter — and coder models aren't tuned for prose.
OLLAMA_MODEL = "qwen3.5:9b"

# Thinking models (qwen3.5, qwen3.6, ...) burn a lot of time "reasoning" before
# answering, which is pure waste for translation and can pollute the JSON reply.
# When True the translator asks Ollama to disable it; models that don't support
# the flag simply get the request retried without it.
OLLAMA_DISABLE_THINKING = True

# Text nodes are grouped into batches of roughly this many characters before
# being sent to the LLM in a single request (fewer round-trips = much faster).
TRANSLATE_BATCH_CHARS = 4000
TRANSLATE_TIMEOUT = 300  # seconds; a 30B model on local hardware can be slow

# Ollama defaults to a small context window (4096 tokens). A batch holds the
# prompt AND the full translated JSON reply, so the default silently truncates
# the answer — the model closes the JSON early and whole entries come back
# missing. Give it room for both sides of the exchange.
OLLAMA_NUM_CTX = 16384

# Supported target languages: code -> human-readable instruction for the model.
# "es-LATAM" is neutral Latin American Spanish (no country-specific slang).
TRANSLATE_LANGUAGES = {
    "es-LATAM": "neutral Latin American Spanish (español latinoamericano neutro, "
                "sin modismos ni regionalismos de ningún país específico)",
}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE_URL,
    # User-Agent is intentionally omitted — curl_cffi sets it to match the
    # browser impersonation (safari17_0), and overriding it would cause a
    # TLS-fingerprint/UA mismatch that Akamai detects as a bot.
}

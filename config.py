import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"

# --- PDF ---
# WeasyPrint needs Pango, Cairo and GObject. On Linux and macOS those are
# system packages and the Python module just works. On Windows they are not
# installed by default and they cannot be borrowed from anywhere: the
# standalone build packs them INSIDE the executable, so there is no folder to
# point a DLL search at. Drop that binary at the path below and the PDF plugin
# shells out to it instead of importing the module.
WEASYPRINT_BIN = os.environ.get("WEASYPRINT_BIN") or str(
    BASE_DIR / "tools" / "weasyprint.exe"
)

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

# --- Translation (local NLLB service) ---
# services/translator runs NLLB-200-3.3B on CTranslate2 int8 on the local GPU.
# It is a dedicated encoder-decoder translation model: no system prompt, no
# refusals, no preamble. Text in, translated text out, which is why none of the
# output validation an instruction-following model needs is here.
TRANSLATOR_URL = "http://127.0.0.1:8100"

# A chapter goes over in one or two batched requests. These bound each request
# so it stays inside the service's own limits (200k chars, 1000 items).
TRANSLATE_REQUEST_CHARS = 150_000
TRANSLATE_REQUEST_ITEMS = 500
TRANSLATE_TIMEOUT = 600  # a long chapter is a few hundred sentences

# Supported target languages: code -> label shown in the UI.
# web/server.py validates the requested language against these keys.
TRANSLATE_LANGUAGES = {
    "es-LATAM": "Español (Latinoamérica)",
}

# code -> FLORES-200 language tag, which is what NLLB speaks. It does not know
# ISO-639: "es" means nothing to it, "spa_Latn" does. FLORES-200 has exactly one
# Spanish, so the neutral-LATAM part is handled by the service's deterministic
# post-edition list rather than by the model.
NLLB_TARGET_LANGS = {
    "es-LATAM": "spa_Latn",
}

# code -> BCP 47, que es lo que hablan EPUB y XHTML.
#
# "es-LATAM" es NUESTRO codigo y no es un tag legal: una subetiqueta de region
# son dos o tres letras, o tres digitos, y "LATAM" tiene cinco. Declararlo tal
# cual metia un tag invalido en cada documento de contenido.
#
# "es" y no "es-419" a proposito. Tipograficamente son identicos -- mismo
# silabeo, mismos glifos -- y un lector que busque su diccionario de silabeo por
# coincidencia exacta encuentra "es" y puede no encontrar "es-419". El matiz
# latinoamericano vive en la lista de post-edicion, no en la tipografia.
BCP47_TAGS = {
    "es-LATAM": "es",
}


def _clean_tag(value: str) -> str:
    """Lo que quede de `value` que pueda aparecer legalmente en un tag BCP 47."""
    raw = str(value or "").strip().lower().split("-")
    primary = "".join(c for c in raw[0] if c.isalpha())[:3]
    if not primary:
        return ""
    if len(raw) > 1:
        region = "".join(c for c in raw[1] if c.isalnum())
        # Region legal = 2-3 letras o 3 digitos. Cualquier otra cosa se descarta
        # en lugar de emitir un tag invalido.
        if len(region) in (2, 3):
            return f"{primary}-{region.upper()}"
    return primary


def language_tag(code: str | None, fallback: str = "en") -> str:
    """Nuestro codigo interno de idioma -> un tag BCP 47 legal.

    `fallback` es el idioma propio del libro: la unica respuesta honesta para
    contenido que no se ha traducido.
    """
    if code and code not in ("original", "en"):
        if code in BCP47_TAGS:
            return BCP47_TAGS[code]
        cleaned = _clean_tag(code)
        if cleaned:
            return cleaned
    return _clean_tag(fallback) or "en"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE_URL,
    # User-Agent is intentionally omitted — curl_cffi sets it to match the
    # browser impersonation (safari17_0), and overriding it would cause a
    # TLS-fingerprint/UA mismatch that Akamai detects as a bot.
}

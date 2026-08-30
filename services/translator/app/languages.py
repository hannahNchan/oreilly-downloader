"""FLORES-200 language codes, the ISO-639 -> FLORES bridge, and resolution.

NLLB does not speak ISO-639. It speaks FLORES-200: a language tag plus a script
tag, `spa_Latn`, not `es`. Passing "es" produces no error, it produces garbage,
because the token simply is not in the vocabulary the way the model expects.

The model itself supports all 200 languages; the table below is an accept-list
for input validation, which is what makes a 400 possible. Add a row and the pair
works, no code change.
"""

from .errors import UnsupportedLanguageError

# FLORES-200 code -> human name. The ~30 most common, English and Spanish first
# because that is this deployment's only real pair.
FLORES: dict[str, str] = {
    "eng_Latn": "English",
    "spa_Latn": "Spanish",
    "fra_Latn": "French",
    "deu_Latn": "German",
    "ita_Latn": "Italian",
    "por_Latn": "Portuguese",
    "nld_Latn": "Dutch",
    "cat_Latn": "Catalan",
    "ron_Latn": "Romanian",
    "pol_Latn": "Polish",
    "ces_Latn": "Czech",
    "hun_Latn": "Hungarian",
    "swe_Latn": "Swedish",
    "dan_Latn": "Danish",
    "nob_Latn": "Norwegian Bokmal",
    "fin_Latn": "Finnish",
    "tur_Latn": "Turkish",
    "ell_Grek": "Greek",
    "rus_Cyrl": "Russian",
    "ukr_Cyrl": "Ukrainian",
    "heb_Hebr": "Hebrew",
    "arb_Arab": "Arabic (Modern Standard)",
    "hin_Deva": "Hindi",
    "ben_Beng": "Bengali",
    "zho_Hans": "Chinese (Simplified)",
    "zho_Hant": "Chinese (Traditional)",
    "jpn_Jpan": "Japanese",
    "kor_Hang": "Korean",
    "vie_Latn": "Vietnamese",
    "ind_Latn": "Indonesian",
    "tha_Thai": "Thai",
}

# ISO-639 (and the loose tags people actually type) -> FLORES-200.
# Keys are compared lowercased with "_" normalised to "-".
ISO_TO_FLORES: dict[str, str] = {
    "en": "eng_Latn", "en-us": "eng_Latn", "en-gb": "eng_Latn", "eng": "eng_Latn",
    # The oreilly-ingest app's own code for its only target language. NLLB has a
    # single Spanish, so the LATAM part of the request is honoured by the
    # post-edition list in postedit.py, not by the model.
    "es": "spa_Latn", "es-latam": "spa_Latn", "es-419": "spa_Latn",
    "es-mx": "spa_Latn", "es-es": "spa_Latn", "es-ar": "spa_Latn", "spa": "spa_Latn",
    "fr": "fra_Latn", "fra": "fra_Latn", "fre": "fra_Latn",
    "de": "deu_Latn", "deu": "deu_Latn", "ger": "deu_Latn",
    "it": "ita_Latn", "ita": "ita_Latn",
    "pt": "por_Latn", "pt-br": "por_Latn", "pt-pt": "por_Latn", "por": "por_Latn",
    "nl": "nld_Latn", "nld": "nld_Latn", "dut": "nld_Latn",
    "ca": "cat_Latn", "cat": "cat_Latn",
    "ro": "ron_Latn", "ron": "ron_Latn",
    "pl": "pol_Latn", "pol": "pol_Latn",
    "cs": "ces_Latn", "ces": "ces_Latn", "cze": "ces_Latn",
    "hu": "hun_Latn", "hun": "hun_Latn",
    "sv": "swe_Latn", "swe": "swe_Latn",
    "da": "dan_Latn", "dan": "dan_Latn",
    "no": "nob_Latn", "nb": "nob_Latn", "nob": "nob_Latn",
    "fi": "fin_Latn", "fin": "fin_Latn",
    "tr": "tur_Latn", "tur": "tur_Latn",
    "el": "ell_Grek", "ell": "ell_Grek", "gre": "ell_Grek",
    "ru": "rus_Cyrl", "rus": "rus_Cyrl",
    "uk": "ukr_Cyrl", "ukr": "ukr_Cyrl",
    "he": "heb_Hebr", "iw": "heb_Hebr", "heb": "heb_Hebr",
    "ar": "arb_Arab", "ara": "arb_Arab",
    "hi": "hin_Deva", "hin": "hin_Deva",
    "bn": "ben_Beng", "ben": "ben_Beng",
    "zh": "zho_Hans", "zh-cn": "zho_Hans", "zh-hans": "zho_Hans", "chi": "zho_Hans",
    "zh-tw": "zho_Hant", "zh-hk": "zho_Hant", "zh-hant": "zho_Hant",
    "ja": "jpn_Jpan", "jpn": "jpn_Jpan",
    "ko": "kor_Hang", "kor": "kor_Hang",
    "vi": "vie_Latn", "vie": "vie_Latn",
    "id": "ind_Latn", "ind": "ind_Latn",
    "th": "tha_Thai", "tha": "tha_Thai",
}

# FLORES -> the code pysbd wants for sentence segmentation. pysbd covers far
# fewer languages than NLLB; anything missing here falls back to English rules,
# which is the safe default for Latin-script prose and is only ever used on the
# SOURCE side (always English in this deployment).
FLORES_TO_PYSBD: dict[str, str] = {
    "eng_Latn": "en",
    "spa_Latn": "es",
    "fra_Latn": "fr",
    "deu_Latn": "de",
    "ita_Latn": "it",
    "nld_Latn": "nl",
    "dan_Latn": "da",
    "pol_Latn": "pl",
    "rus_Cyrl": "ru",
    "ell_Grek": "el",
    "arb_Arab": "ar",
    "hin_Deva": "hi",
    "ben_Beng": "bn",
    "zho_Hans": "zh",
    "zho_Hant": "zh",
    "jpn_Jpan": "ja",
    "urd_Arab": "ur",
}

# Case-insensitive index over FLORES so "SPA_LATN" and "spa_latn" both resolve.
_FLORES_CI = {code.lower(): code for code in FLORES}


def resolve(code: str | None, default: str) -> str:
    """Return a FLORES-200 code, accepting FLORES or ISO input.

    An empty or missing code means "use the default", which is what lets a
    caller POST {"text": "..."} with no language fields at all.
    """
    if code is None or not str(code).strip():
        return default

    raw = str(code).strip()

    exact = _FLORES_CI.get(raw.lower())
    if exact:
        return exact

    iso = ISO_TO_FLORES.get(raw.lower().replace("_", "-"))
    if iso:
        return iso

    raise UnsupportedLanguageError(
        f"Unsupported language code '{raw}'. Use a FLORES-200 code "
        f"(spa_Latn, eng_Latn, ...) or a common ISO-639 code (es, en, ...).",
        supported_flores=sorted(FLORES),
    )


def pysbd_code(flores: str) -> str:
    """pysbd language for a FLORES code, English rules when unsupported."""
    return FLORES_TO_PYSBD.get(flores, "en")

"""Tests for the parts that do not need the GPU, the model, or any dependency.

Deliberately dependency-free: plain asserts, no pytest, runnable with the
system Python before setup.ps1 has installed anything.

    python tests/test_logic.py

What is covered is exactly what is easy to get silently wrong: the segmentation
round trip, the order in which translations come back, and the post-edition
rules where a naive replacement leaves broken Spanish behind.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import postedit, segmenter  # noqa: E402
from app.errors import UnsupportedLanguageError  # noqa: E402
from app.languages import resolve  # noqa: E402

PASSED = 0
FAILED = 0


def check(condition, label, extra=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}")
        if extra:
            print(f"       {extra}")


# --- stubs -----------------------------------------------------------------

_SPANS = re.compile(r"[^.!?]*[.!?]+|[^.!?]+")


def stub_splitter(text):
    """Stand-in for pysbd: contiguous spans ending at sentence punctuation."""
    return [(m.start(), m.end()) for m in _SPANS.finditer(text) if m.group()]


def count_words(text):
    return len([w for w in text.split() if w])


def count_chars(text):
    return len(text)


def identity(chunks):
    return list(chunks)


# --- segmentation ----------------------------------------------------------

def test_exact_round_trip():
    print("\nsegmenter: exact round trip (nothing needs cutting)")
    samples = [
        "Hello world.",
        "First sentence. Second sentence! Third?",
        "  leading and trailing spaces  ",
        "line one.\nline two.\n\nline four.",
        "\n\n",
        "",
        "   ",
        "No final punctuation",
        "A sentence.  Two spaces before this one.",
        "\tindented list item. And more.",
    ]
    for text in samples:
        plans, chunks = segmenter.plan([text], stub_splitter, count_words, 100)
        rebuilt = segmenter.assemble_all(plans, identity(chunks))[0]
        check(rebuilt == text, f"round trip {text!r}", f"got {rebuilt!r}")


def test_chunks_are_clean():
    print("\nsegmenter: chunks handed to the model")
    text = "  First one.   Second one.  "
    _, chunks = segmenter.plan([text], stub_splitter, count_words, 100)
    check(chunks == ["First one.", "Second one."], "stripped, no whitespace chunks", f"got {chunks}")


def test_order_across_texts():
    print("\nsegmenter: order is preserved across a batch")
    texts = ["One. Two.", "Three.", "", "Four. Five. Six."]
    plans, chunks = segmenter.plan(texts, stub_splitter, count_words, 100)
    check(
        chunks == ["One.", "Two.", "Three.", "Four.", "Five.", "Six."],
        "flat chunk list is in reading order",
        f"got {chunks}",
    )
    translated = [c.upper() for c in chunks]
    rebuilt = segmenter.assemble_all(plans, translated)
    check(
        rebuilt == ["ONE. TWO.", "THREE.", "", "FOUR. FIVE. SIX."],
        "each text gets its own translations back",
        f"got {rebuilt}",
    )


def test_long_sentence_is_cut():
    print("\nsegmenter: an over-long sentence gets cut")
    # 60 words, no internal punctuation, limit of 10 words.
    text = " ".join(f"word{i}" for i in range(60)) + "."
    plans, chunks = segmenter.plan([text], stub_splitter, count_words, 10)
    check(len(chunks) > 1, f"cut into {len(chunks)} chunks")
    check(
        all(count_words(c) <= 10 for c in chunks),
        "every chunk is under the limit",
        f"sizes {[count_words(c) for c in chunks]}",
    )
    rebuilt = segmenter.assemble_all(plans, identity(chunks))[0]
    check(rebuilt.split() == text.split(), "content survives the cut", f"got {rebuilt[:60]!r}")


def test_clause_split_keeps_punctuation():
    print("\nsegmenter: clause cuts keep their punctuation")
    left = " ".join(f"a{i}" for i in range(12))
    right = " ".join(f"b{i}" for i in range(12))
    text = f"{left}; {right}."
    plans, chunks = segmenter.plan([text], stub_splitter, count_words, 15)
    check(len(chunks) == 2, f"cut at the semicolon into {len(chunks)} chunks", f"{chunks}")
    check(chunks[0].endswith(";"), "the semicolon stays with the left half", f"{chunks[0][-20:]!r}")
    rebuilt = segmenter.assemble_all(plans, identity(chunks))[0]
    check(rebuilt == text, "exact round trip through a single-space cut", f"got {rebuilt!r}")


def whole_text_splitter(text):
    """One span covering everything: isolates the long-sentence path."""
    return [(0, len(text))]


def test_oversized_token_passes_through():
    print("\nsegmenter: an untranslatable blob is not sent to the model")
    # No spaces anywhere, so there is nothing to cut on: a URL, a base64 blob,
    # a generated identifier. Slicing it would corrupt it and translating it is
    # pointless.
    text = "https://example.com/" + "x" * 200
    plans, chunks = segmenter.plan([text], whole_text_splitter, count_chars, 20)
    check(chunks == [], "nothing was sent to the model", f"got {chunks}")
    rebuilt = segmenter.assemble_all(plans, [])[0]
    check(rebuilt == text, "passed through verbatim", f"got {rebuilt[:40]!r}")


def test_count_mismatch_is_explicit():
    print("\nsegmenter: a translation-count mismatch says so")
    plans, chunks = segmenter.plan(["One. Two. Three."], stub_splitter, count_words, 100)
    check(segmenter.expected_chunks(plans) == 3, "expected_chunks agrees with plan()")
    try:
        segmenter.assemble_all(plans, chunks[:2])
        check(False, "should have refused two translations for three chunks")
    except ValueError as exc:
        check("needs 3" in str(exc) and "got 2" in str(exc),
              "the error names both counts", f"got {exc}")


def test_broken_splitter_loses_nothing():
    print("\nsegmenter: a misbehaving splitter cannot lose text")

    def overlapping(text):
        return [(0, 10), (5, 20), (0, 3)]  # overlaps and goes backwards

    text = "abcdefghijklmnopqrstuvwxyz"
    plans, chunks = segmenter.plan([text], overlapping, count_words, 100)
    rebuilt = segmenter.assemble_all(plans, identity(chunks))[0]
    check(rebuilt == text, "every character survives", f"got {rebuilt!r}")

    def exploding(text):
        raise RuntimeError("segmenter blew up")

    plans, chunks = segmenter.plan([text], exploding, count_words, 100)
    rebuilt = segmenter.assemble_all(plans, identity(chunks))[0]
    check(rebuilt == text, "a raising splitter still returns the text", f"got {rebuilt!r}")


def test_pysbd_if_available():
    print("\nsegmenter: pysbd round trip (skipped if not installed)")
    try:
        import pysbd  # noqa: F401
    except ImportError:
        print("  skip pysbd not installed yet")
        return
    splitter = segmenter.make_pysbd_splitter("en")
    samples = [
        "Dr. Smith went to Washington. He arrived at 3 p.m. on Tuesday.",
        "A class is a blueprint.\n\nThe next paragraph follows.",
        "  Indented.  Then more.  ",
    ]
    for text in samples:
        plans, chunks = segmenter.plan([text], splitter, count_words, 200)
        rebuilt = segmenter.assemble_all(plans, identity(chunks))[0]
        check(rebuilt == text, f"pysbd round trip {text[:32]!r}", f"got {rebuilt!r}")


# --- post-edition ----------------------------------------------------------

def test_postedit_rules():
    print("\npostedit: the real rule file")
    path = ROOT / "data" / "postedit_spa_Latn.txt"
    check(path.is_file(), f"rule file exists at {path.name}")
    if not path.is_file():
        return
    editor = postedit.load(path)
    check(editor.size > 0, f"{editor.size} rules loaded")

    cases = [
        # (input, expected, why)
        ("Abre el fichero de configuracion.",
         "Abre el archivo de configuracion.",
         "fichero -> archivo"),
        ("Los ficheros estan aqui.",
         "Los archivos estan aqui.",
         "plural"),
        ("Fichero no encontrado.",
         "Archivo no encontrado.",
         "capital is carried over"),
        ("Reinicia el ordenador.",
         "Reinicia la computadora.",
         "the article changes with the gender"),
        ("La memoria del ordenador.",
         "La memoria de la computadora.",
         "del ordenador -> de la computadora"),
        ("Conecta los ordenadores.",
         "Conecta las computadoras.",
         "plural with article"),
        ("Puedes coger el valor de la lista.",
         "Puedes tomar el valor de la lista.",
         "coger -> tomar"),
        ("Vamos a recoger los datos.",
         "Vamos a recoger los datos.",
         "recoger is NOT touched (word boundary)"),
        ("Si vosotros teneis dudas.",
         "Si ustedes tienen dudas.",
         "vosotros forms"),
        ("El desarrollo movil es distinto.",
         "El desarrollo movil es distinto.",
         "adjectival 'movil' is left alone on purpose"),
        ("Usa tu telefono movil.",
         "Usa tu telefono celular.",
         "only the full phrase is replaced"),
        ("El OS no se toca.",
         "El OS no se toca.",
         "'os => les' is commented out, so OS survives"),
    ]
    for source, expected, why in cases:
        got = editor.apply(source)
        check(got == expected, why, f"{source!r} -> {got!r}, wanted {expected!r}")


def test_postedit_order_matters():
    print("\npostedit: file order decides the outcome")
    specific_first = postedit.PostEditor([("el ordenador", "la computadora"), ("ordenador", "computadora")])
    general_first = postedit.PostEditor([("ordenador", "computadora"), ("el ordenador", "la computadora")])
    check(
        specific_first.apply("el ordenador") == "la computadora",
        "specific rule first gives correct Spanish",
    )
    check(
        general_first.apply("el ordenador") == "el computadora",
        "general rule first leaves the article stranded (this is why order matters)",
    )


def test_postedit_parsing():
    print("\npostedit: rule file parsing")
    lines = [
        "# a comment",
        "",
        "   ",
        "uno => dos",
        "  tres   =>   cuatro  ",
        "no arrow here",
        "=> missing source",
        "missing target =>",
    ]
    rules = postedit.parse(lines)
    check(rules == [("uno", "dos"), ("tres", "cuatro")], "only well-formed rules survive", f"got {rules}")


# --- languages -------------------------------------------------------------

def test_language_resolution():
    print("\nlanguages: FLORES and ISO resolution")
    cases = [
        (None, "eng_Latn", "None falls back to the default"),
        ("", "eng_Latn", "empty falls back to the default"),
        ("spa_Latn", "spa_Latn", "FLORES passes through"),
        ("SPA_LATN", "spa_Latn", "FLORES is case-insensitive"),
        ("es", "spa_Latn", "ISO 639-1"),
        ("es-LATAM", "spa_Latn", "the app's own code"),
        ("es_latam", "spa_Latn", "underscore variant"),
        ("en", "eng_Latn", "English"),
        ("zh-TW", "zho_Hant", "script-specific Chinese"),
        ("pt-BR", "por_Latn", "regional Portuguese"),
    ]
    for code, expected, why in cases:
        got = resolve(code, "eng_Latn")
        check(got == expected, why, f"{code!r} -> {got!r}, wanted {expected!r}")

    for bad in ["klingon", "es-XX-YY", "xx", "spa-Latn-extra"]:
        try:
            resolve(bad, "eng_Latn")
            check(False, f"{bad!r} should be rejected")
        except UnsupportedLanguageError as exc:
            check(exc.status == 400, f"{bad!r} rejected with 400")


# --- runner ----------------------------------------------------------------

def main():
    print("=" * 72)
    print("NLLB translation service: logic tests (no GPU, no model, no deps)")
    print("=" * 72)

    test_exact_round_trip()
    test_chunks_are_clean()
    test_order_across_texts()
    test_long_sentence_is_cut()
    test_clause_split_keeps_punctuation()
    test_oversized_token_passes_through()
    test_count_mismatch_is_explicit()
    test_broken_splitter_loses_nothing()
    test_pysbd_if_available()
    test_postedit_rules()
    test_postedit_order_matters()
    test_postedit_parsing()
    test_language_resolution()

    print("\n" + "=" * 72)
    print(f"{PASSED} passed, {FAILED} failed")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

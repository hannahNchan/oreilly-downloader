"""Sentence segmentation and exact reassembly.

NLLB is a sentence-level model trained with a ~512 token limit. Feeding it a
whole paragraph does not raise an error; it quietly translates the beginning
and drops the rest. So segmentation is not an optimisation, it is a
correctness requirement.

The contract this module keeps: with an identity translation, reassembly
returns the input character for character. Everything that is not part of a
sentence (the whitespace between them, a leading newline, the indentation of a
list item) travels through as a Gap and is put back where it was.

One documented exception: a sentence long enough to need cutting is rejoined
with a single space at each cut point, so a run of whitespace sitting exactly on
a cut collapses. That affects only sentences over ~480 tokens, which in book
prose means a mangled table or a run-on line from a bad PDF conversion.

tests/test_logic.py asserts both halves of that: exact identity for text that
fits, whitespace-insensitive identity for text that had to be cut.

pysbd does the splitting, with clean=False (mandatory: clean=True rewrites the
text, which makes exact reassembly impossible) and char_span=True, so we get
offsets into the original string instead of pysbd's own idea of the sentence.
"""

import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

# A "sentence" can still blow past the token limit: a table row flattened into
# prose, a run-on line from a badly converted PDF, a paragraph with no full
# stop. Those get a second cut, on clause boundaries first, because the model
# translates a clause far better than an arbitrary slice of words.
# Priority order. The separator's punctuation stays with the left half.
# — em dash, – en dash: publisher prose is full of both.
_CLAUSE_SEPARATORS = ("; ", ": ", " — ", " – ", ", ")
_MAX_SPLIT_DEPTH = 6

SplitSentences = Callable[[str], list[tuple[int, int]]]
CountTokens = Callable[[str], int]


@dataclass(frozen=True)
class Gap:
    """Text never sent to the model: whitespace, or an untranslatable run."""

    text: str


@dataclass(frozen=True)
class Chunk:
    """One unit handed to the model. translate=False means pass through as-is."""

    text: str
    translate: bool = True


@dataclass
class Sentence:
    """One sentence, as one or more chunks if it had to be cut."""

    chunks: list[Chunk] = field(default_factory=list)


Segment = Gap | Sentence


# --- pysbd backend ---------------------------------------------------------

def make_pysbd_splitter(language: str = "en") -> SplitSentences:
    """Return a thread-safe sentence splitter for `language`.

    pysbd.Segmenter keeps the text it is working on as instance state, so one
    shared instance across threads corrupts results under concurrency. A
    thread-local instance costs one regex compile per thread and removes the
    problem entirely.
    """
    import pysbd

    store = threading.local()

    def split(text: str) -> list[tuple[int, int]]:
        segmenter = getattr(store, "segmenter", None)
        if segmenter is None:
            try:
                segmenter = pysbd.Segmenter(
                    language=language, clean=False, char_span=True
                )
            except Exception:
                # pysbd covers far fewer languages than NLLB. English rules are
                # a sane fallback for Latin-script prose, and this only ever
                # affects the source side.
                segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
            store.segmenter = segmenter
        return [(span.start, span.end) for span in segmenter.segment(text)]

    return split


# --- long-sentence splitting ----------------------------------------------

def _middlemost(text: str, separator: str) -> int | None:
    """Index of the occurrence of `separator` closest to the middle.

    Splitting near the middle keeps both halves balanced. A 500/20 split just
    moves the problem into the next recursion.
    """
    best = None
    best_distance = None
    middle = len(text) // 2
    start = 0
    while True:
        found = text.find(separator, start)
        if found == -1:
            break
        start = found + 1
        if found == 0 or found + len(separator) >= len(text):
            continue  # a split there produces an empty half
        distance = abs(found - middle)
        if best_distance is None or distance < best_distance:
            best, best_distance = found, distance
    return best


def _split_by_words(text: str, count_tokens: CountTokens, target: int) -> list[Chunk]:
    """Last resort: greedy word windows that fit inside `target` tokens."""
    words = text.split(" ")
    chunks: list[Chunk] = []
    current: list[str] = []

    for word in words:
        candidate = current + [word]
        if current and count_tokens(" ".join(candidate)) > target:
            chunks.append(Chunk(" ".join(current)))
            current = [word]
        else:
            current = candidate

    if current:
        joined = " ".join(current)
        # A single "word" over the limit is not prose: a URL, a base64 blob, a
        # very long identifier. Slicing it would corrupt it and translating it
        # is pointless, so it travels through untouched instead of being
        # silently truncated by the model.
        translate = " " in joined or count_tokens(joined) <= target
        chunks.append(Chunk(joined, translate=translate))

    return chunks


def _chunks_for(
    text: str, count_tokens: CountTokens, target: int, depth: int = 0
) -> list[Chunk]:
    if count_tokens(text) <= target:
        return [Chunk(text)]

    if depth < _MAX_SPLIT_DEPTH:
        for separator in _CLAUSE_SEPARATORS:
            index = _middlemost(text, separator)
            if index is None:
                continue
            left = text[: index + len(separator)].rstrip()
            right = text[index + len(separator):].lstrip()
            if not left or not right:
                continue
            return _chunks_for(left, count_tokens, target, depth + 1) + _chunks_for(
                right, count_tokens, target, depth + 1
            )

    return _split_by_words(text, count_tokens, target)


# --- planning and reassembly ----------------------------------------------

def split_text(
    text: str,
    split_sentences: SplitSentences,
    count_tokens: CountTokens,
    target: int,
) -> list[Segment]:
    """Break `text` into Gaps and Sentences covering every character once."""
    if not text:
        return []

    try:
        spans = split_sentences(text)
    except Exception:
        # A segmenter failure must not lose text. One oversized sentence, still
        # cut to size below, is worse output than proper segmentation but it is
        # not data loss.
        spans = [(0, len(text))]

    segments: list[Segment] = []
    cursor = 0

    for start, end in spans:
        # pysbd offsets are trusted but not assumed: clamping means overlapping
        # or out-of-order spans can never duplicate or drop characters.
        start = max(start, cursor)
        end = min(max(end, cursor), len(text))
        if end <= start:
            continue

        if start > cursor:
            segments.append(Gap(text[cursor:start]))

        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            segments.append(Gap(raw))
        else:
            lead = raw[: len(raw) - len(raw.lstrip())]
            trail = raw[len(raw.rstrip()):]
            if lead:
                segments.append(Gap(lead))
            segments.append(Sentence(_chunks_for(stripped, count_tokens, target)))
            if trail:
                segments.append(Gap(trail))

        cursor = end

    if cursor < len(text):
        segments.append(Gap(text[cursor:]))

    return segments


def plan(
    texts: list[str],
    split_sentences: SplitSentences,
    count_tokens: CountTokens,
    target: int,
) -> tuple[list[list[Segment]], list[str]]:
    """Segment every text and return one flat chunk list for the whole request.

    Flattening across texts is the point: a batch of 400 short paragraphs
    becomes a handful of full GPU calls instead of 400 small ones.
    """
    plans = [split_text(text, split_sentences, count_tokens, target) for text in texts]
    chunks = [
        chunk.text
        for segments in plans
        for segment in segments
        if isinstance(segment, Sentence)
        for chunk in segment.chunks
        if chunk.translate
    ]
    return plans, chunks


def assemble(segments: list[Segment], translated: Iterator[str]) -> str:
    """Rebuild one text, pulling translations in the order plan() produced."""
    out: list[str] = []
    for segment in segments:
        if isinstance(segment, Gap):
            out.append(segment.text)
            continue
        parts = [
            next(translated) if chunk.translate else chunk.text
            for chunk in segment.chunks
        ]
        out.append(" ".join(part for part in parts if part))
    return "".join(out)


def expected_chunks(plans: list[list[Segment]]) -> int:
    """How many translations assemble_all is going to consume."""
    return sum(
        1
        for segments in plans
        for segment in segments
        if isinstance(segment, Sentence)
        for chunk in segment.chunks
        if chunk.translate
    )


def assemble_all(plans: list[list[Segment]], translated: list[str]) -> list[str]:
    """Rebuild every text.

    The count is checked before rebuilding rather than being discovered
    half-way: running short raises a bare StopIteration out of a list
    comprehension, which says nothing at all about what went wrong.
    """
    wanted = expected_chunks(plans)
    if len(translated) != wanted:
        raise ValueError(
            f"translation count mismatch: the plan needs {wanted} chunks, "
            f"got {len(translated)}"
        )
    stream = iter(translated)
    return [assemble(segments, stream) for segments in plans]

"""Translation plugin: translate chapter HTML via the local NLLB service.

The service lives in services/translator (NLLB-200-3.3B on CTranslate2, int8,
on the local GPU). See its README for setup.

THE PROBLEM
-----------
NLLB is an encoder-decoder translation model. It has no system prompt and no
concept of a rule, so it cannot be asked to preserve markup. Feed it HTML and
the HTML comes back mangled. The tags have to be taken out of its way and put
back afterwards.

So each leaf block becomes a template where every piece of markup is a short
numeric placeholder:

    <p>The <code>pd.Series</code> class is a <i>blueprint</i>.</p>
      encodes to
    "The %%0%% class is a %%1%%blueprint%%2%%."

The model sees the whole sentence, which is what it needs to get Spanish word
order right, and it carries the placeholders along with the words they belong
to. Then the markup goes back in. Protected content is *never sent*: it cannot
be corrupted because it never leaves this process.

WHAT WAS MEASURED (not assumed)
-------------------------------
Probing the running service:

- The source's line wrapping was costing whole paragraphs, and that was a bug
  here rather than in the model. Publisher HTML wraps its lines, so a template
  arrived with newlines mid-sentence; a placeholder stranded at the start of a
  wrapped line got silently dropped and the block lost its translation.
  Collapsing the wrapping before sending fixed it. Biggest single cause of loss.
- Opaque placeholders beat real tag names. Given
  "Choose <i>one</i>, <i>two</i> or <i>three</i>." the model returned
  "Elige uno, dos o tres." -- flawless, and stripped of every tag. It
  *recognises* markup and feels free to discard it, while a placeholder is
  meaningless text it simply copies. Adversarial probe: 6/9 against 5/9.
- Loss is still real, and does not scale with how many placeholders there are:
  12 survived a sentence where 6 did not. The trigger is heavy restructuring,
  e.g. "See X for Y" -> "Vease X para Y". Changing the beam width rescues
  nothing (0 of 4 failing cases at beam 1 and beam 2).
- After the whitespace fix, a 13-block chapter sample carrying 18 placeholders
  came back with every protected placeholder intact and one formatting pair
  lost. scripts/check_translate_markup.py is that check, and its encoder
  assertions run with no GPU and no service.

IDEAS TAKEN FROM oomol-lab/epub-translator
------------------------------------------
That project solves the same problem surgically, for an instruction-following
model. Three of its ideas transfer; one does not.

1. Blocks are "everything that is not inline", derived from the MDN inline-level
   element list, instead of a hand-written list of block tags. Exhaustive, and
   it fixes a real bug: a <div> holding only inline content used to fall through
   to per-text-node translation, which handed the model blind fragments.
2. Markup is restored from the ORIGINAL element, never from what the model
   returned. An href cannot be corrupted because it is never sent.
3. Best-effort reassembly instead of all-or-nothing. This is the important one.
   When "Elige uno, dos o tres." came back without its italics, the old
   behaviour discarded it and left the reader with English. Losing italics is a
   far smaller loss than losing the translation.

   So placeholders are classified. PROTECTED ones (code, images, formulas) hold
   content that would vanish with them, so if one is lost the block is rejected
   and retried. FORMATTING ones (i, b, a, sup) hold only appearance: if one is
   lost, the translation is kept and that formatting is dropped.

4. NOT taken: their real-tag-name-with-minimal-ids encoding. It depends on a
   model that can be told to preserve tags, and the measurement above shows it
   is actively worse here.

THE LADDER
----------
    1. full     -- protected content plus formatting pairs
    2. bare     -- protected content only, formatting flattened: far fewer
                   placeholders, so the code has the best chance of surviving.
                   Skipped when there is no formatting to flatten, because the
                   template would come out identical and beam search is
                   deterministic: same input, same dropped placeholder
    3. give up  -- the block keeps its original text

Each rung is one batched request per chapter, not one per block. Failure is
non-fatal throughout: if the service is unreachable or a request fails, the
original markup is kept so a download never breaks because of translation.
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html import escape

from bs4 import BeautifulSoup, NavigableString

import config
from .base import Plugin

# HTML inline-level elements, from MDN's inline-level content list plus MathML.
# Everything NOT in here is a block, which is how leaf blocks get found.
# Source of the idea and of the list: oomol-lab/epub-translator, xml/inline.py.
INLINE_TAGS = frozenset({
    # inline text semantics
    "a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "data", "dfn", "em",
    "i", "kbd", "mark", "q", "rp", "rt", "ruby", "s", "samp", "small", "span",
    "strong", "sub", "sup", "time", "u", "var", "wbr", "tt",
    # image and multimedia
    "img", "svg", "canvas", "audio", "video", "map", "area", "picture",
    # form elements
    "input", "button", "select", "textarea", "label", "output", "progress",
    "meter",
    # embedded content
    "iframe", "embed", "object",
    # other
    "script", "del", "ins", "slot",
    # MathML
    "math", "mi", "mn", "mo", "ms", "mspace", "mtext", "menclose", "merror",
    "mfenced", "mfrac", "mpadded", "mphantom", "mroot", "mrow", "msqrt",
    "mstyle", "mmultiscripts", "mover", "mprescripts", "msub", "msubsup",
    "msup", "munder", "munderover", "mtable", "mtr", "mtd", "annotation",
    "annotation-xml", "semantics", "maction",
})

# Content that must never be translated AND must never be lost. One placeholder
# each, holding the element's whole markup. If one of these does not come back,
# the block is rejected: dropping it would delete a code span or an image.
PROTECTED_TAGS = frozenset({
    "pre", "code", "kbd", "samp", "var", "tt", "script", "style",
    "img", "svg", "math", "iframe", "object", "embed", "audio", "video",
    "canvas", "picture",
})

# Appearance only. A pair of placeholders, and losing them costs a bit of
# formatting, not content, so the translation is kept anyway.
FORMATTING_TAGS = frozenset({
    "a", "i", "b", "em", "strong", "span", "sup", "sub", "abbr", "cite", "q",
    "small", "u", "mark", "dfn", "s", "del", "ins", "time", "data", "bdi",
    "bdo", "ruby", "rt", "rp",
})

# Void and optional: losing a <br> merges two lines. Cosmetic.
OPTIONAL_VOID_TAGS = frozenset({"br", "wbr", "hr"})

# Built with an f-string, NOT printf formatting. "%%%d%%" % 0 yields "%0%", not
# "%%0%%": printf reads each "%%" as one literal percent. That silently produced
# a different marker from the one that was benchmarked, and left the scrub regex
# below unable to match its own placeholders. Keep these two in agreement; the
# check in scripts/check_translate_markup.py asserts that they are.
def placeholder_for(index: int) -> str:
    return f"%%{index}%%"


_PLACEHOLDER_RE = re.compile(r"%%\d+%%")

_WHITESPACE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """Squeeze runs of whitespace to one space.

    Publisher HTML wraps its source lines, so a paragraph arrives with newlines
    sitting in the middle of sentences. Those newlines are invisible in HTML --
    every renderer collapses them -- but they are NOT invisible to the model:
    measured on a real chapter, a placeholder that ended up alone at the start
    of a wrapped line was silently dropped, while the same paragraph on one line
    kept every placeholder. So the wrapping is removed before sending.

    Safe because the one place whitespace is significant, <pre>, is protected
    content: it travels inside a placeholder and never appears in the template.
    Collapsing to a space rather than stripping matters too -- a text node that
    is nothing but a newline between two inline tags is a word boundary, and
    deleting it would glue two words together.
    """
    return _WHITESPACE.sub(" ", text)

# The model sometimes shifts a space across a placeholder boundary, which after
# substitution reads as "<i> word</i>". Cosmetic, and trivial to undo.
_FORMATTING_ALT = "|".join(sorted(FORMATTING_TAGS))
_OPEN_SPACE = re.compile(r"(<(?:%s)\b[^>]*>)(\s+)" % _FORMATTING_ALT)
_CLOSE_SPACE = re.compile(r"(\s+)(</(?:%s)\s*>)" % _FORMATTING_ALT)


def is_inline_name(name: str | None) -> bool:
    return bool(name) and name.lower() in INLINE_TAGS


@dataclass
class _Unit:
    """One thing to translate, and how to put the answer back."""

    kind: str                      # "block" or "orphan"
    element: object = None         # leaf block, for kind == "block"
    node: object = None            # NavigableString, for kind == "orphan"
    lead: str = ""                 # whitespace to restore around an orphan
    trail: str = ""
    template: str = ""
    slots: list = field(default_factory=list)      # index -> markup to restore
    required: set = field(default_factory=set)     # indices that must survive
    pairs: list = field(default_factory=list)      # (open index, close index)
    full_markup: bool = True
    degraded: bool = False         # translated, but some formatting was lost


class TranslatorPlugin(Plugin):
    """Translate chapter HTML into a target language using the local NLLB service."""

    # Kept as attributes because other code and tests reach for them.
    PROTECTED_TAGS = PROTECTED_TAGS
    INLINE_TAGS = INLINE_TAGS

    # --- availability -----------------------------------------------------

    def is_available(self) -> bool:
        """True if the service answers and has the model loaded.

        `loaded` is what matters, not the HTTP status: the service starts even
        when the model failed so that /health can explain why. A 503 with a
        reason is more useful than a dead port, but it still means no
        translation.
        """
        try:
            data = self._get("/health", timeout=5)
        except Exception:
            return False
        return bool((data or {}).get("model", {}).get("loaded"))

    # --- entry point ------------------------------------------------------

    def translate_html(self, html: str, target_lang: str) -> str:
        """Translate the prose of an HTML fragment, preserving its markup.

        Returns the original html unchanged if target_lang is unknown, or if
        translation fails.
        """
        flores = config.NLLB_TARGET_LANGS.get(target_lang)
        if not flores:
            return html

        soup = BeautifulSoup(html, "lxml")

        blocks = self._collect_leaf_blocks(soup)
        # Text outside every block still needs translating; collect it before
        # block contents are swapped out.
        orphans = self._collect_orphan_text_nodes(soup, blocks)

        if not blocks and not orphans:
            return html

        units: list[_Unit] = []
        for element in blocks:
            unit = self._encode(element, full_markup=True)
            if unit is not None:
                units.append(unit)

        for node, lead, core, trail in orphans:
            units.append(_Unit(
                kind="orphan", node=node, lead=lead, trail=trail,
                template=collapse_whitespace(core),
            ))

        if not units:
            return html

        rejected = self._run_round(units, flores)

        # Rung 2: only the blocks that lost protected content. Re-encoded with
        # formatting flattened, so there are far fewer placeholders and the code
        # spans have the best chance of coming back.
        retry: list[_Unit] = []
        for unit in rejected:
            if unit.kind != "block":
                continue
            bare = self._encode(unit.element, full_markup=False)
            if bare is None:
                continue
            if bare.template == unit.template:
                # Nothing to flatten, so this rung has nothing new to try.
                # Sending it again is a wasted GPU call: same input, same
                # deterministic beam search, same dropped placeholder.
                continue
            retry.append(bare)

        if retry:
            print(f"[TRANSLATE] {len(retry)} bloque(s) reintentados sin formato inline")
            still = self._run_round(retry, flores)
            if still:
                print(f"[TRANSLATE] {len(still)} bloque(s) sin traducir")

        degraded = sum(1 for unit in units if unit.degraded)
        if degraded:
            print(f"[TRANSLATE] {degraded} bloque(s) traducidos con formato inline perdido")

        # BeautifulSoup+lxml wraps a fragment in <html><body>. Unwrap so the
        # output stays a fragment like html_processor.process produced.
        body = soup.body
        if body is not None:
            return "".join(str(child) for child in body.contents)
        return str(soup)

    # --- one batched round -------------------------------------------------

    def _run_round(self, units: list[_Unit], flores: str) -> list[_Unit]:
        """Translate these units in batches and apply what came back.

        Returns the units that had to be rejected outright.
        """
        translations = self._translate([unit.template for unit in units], flores)
        rejected: list[_Unit] = []

        for unit, translated in zip(units, translations):
            if not translated or not translated.strip():
                rejected.append(unit)
                continue
            if not self._apply(unit, translated):
                rejected.append(unit)

        return rejected

    def _apply(self, unit: _Unit, translated: str) -> bool:
        if unit.kind == "orphan":
            unit.node.replace_with(
                NavigableString(f"{unit.lead}{translated}{unit.trail}")
            )
            return True

        fragment = self._decode(translated, unit)
        if fragment is None or not fragment.strip():
            return False
        self._replace_contents(unit.element, fragment)
        return True

    # --- encoding ---------------------------------------------------------

    def _encode(self, element, full_markup: bool) -> "_Unit | None":
        """Turn a block into a template plus the markup its placeholders hold.

        full_markup=True keeps formatting as placeholder pairs. False drops
        them, keeping only their text, which leaves far fewer placeholders for
        the model to lose. Returns None when there is no prose to translate.
        """
        parts: list[str] = []
        slots: list[str] = []
        required: set[int] = set()
        pairs: list[tuple[int, int]] = []

        def placeholder(markup: str, is_required: bool) -> int:
            slots.append(markup)
            index = len(slots) - 1
            if is_required:
                required.add(index)
            parts.append(placeholder_for(index))
            return index

        def walk(node) -> None:
            for child in node.children:
                if isinstance(child, NavigableString):
                    parts.append(collapse_whitespace(str(child)))
                    continue

                name = getattr(child, "name", None)
                if name is None:
                    continue
                name = name.lower()

                if name in PROTECTED_TAGS:
                    # Whole element as one placeholder. Its text never reaches
                    # the model, and losing it would delete content, so it is
                    # required.
                    placeholder(str(child), True)
                    continue

                if name in OPTIONAL_VOID_TAGS:
                    placeholder(str(child), False)
                    continue

                if full_markup and name in FORMATTING_TAGS:
                    opening = placeholder(_open_tag(child), False)
                    walk(child)
                    closing = placeholder(f"</{name}>", False)
                    pairs.append((opening, closing))
                    continue

                # Anything else: keep the text, drop the tag.
                walk(child)

        walk(element)
        template = "".join(parts).strip()

        if not _PLACEHOLDER_RE.sub("", template).strip():
            return None  # nothing but markup and whitespace in here

        return _Unit(
            kind="block",
            element=element,
            template=template,
            slots=slots,
            required=required,
            pairs=pairs,
            full_markup=full_markup,
        )

    # --- decoding, best effort --------------------------------------------

    def _decode(self, translated: str, unit: _Unit) -> "str | None":
        """Put the markup back, salvaging what the model lost.

        Returns None only when protected content did not survive, because
        substituting it is the only way its text gets back into the document.
        Everything else degrades: a formatting pair that came back broken is
        dropped and the translated words are kept.
        """
        for index in unit.required:
            if translated.count(placeholder_for(index)) != 1:
                return None

        text = translated
        dropped: set[int] = set()

        for opening, closing in unit.pairs:
            intact = (
                text.count(placeholder_for(opening)) == 1
                and text.count(placeholder_for(closing)) == 1
            )
            if intact and text.find(placeholder_for(opening)) > text.find(
                placeholder_for(closing)
            ):
                # A closing tag that overtook its opening tag would produce
                # broken HTML. Reordering as such is fine and expected: Spanish
                # word order differs, and a pair that travels together lands
                # somewhere correct.
                intact = False
            if not intact:
                dropped.add(opening)
                dropped.add(closing)
                unit.degraded = True

        for index, markup in enumerate(unit.slots):
            if index in dropped:
                continue
            text = text.replace(placeholder_for(index), markup, 1)

        # Anything left is a placeholder the model duplicated, or half of a pair
        # that was dropped. Neither belongs in the output.
        leftover = _PLACEHOLDER_RE.search(text)
        if leftover:
            unit.degraded = True
            text = _PLACEHOLDER_RE.sub("", text)

        text = _OPEN_SPACE.sub(r"\2\1", text)
        text = _CLOSE_SPACE.sub(r"\2\1", text)
        return text

    # --- HTTP -------------------------------------------------------------

    def _translate(self, texts: list[str], flores: str) -> list:
        """Translate a list of templates. Failed chunks come back as None.

        Chunk granularity for failure is deliberate: one bad request should cost
        those paragraphs, not the whole chapter.
        """
        out: list = []
        for chunk in self._chunks(texts):
            result = self._post_batch(chunk, flores)
            if result is None or len(result) != len(chunk):
                out.extend([None] * len(chunk))
            else:
                out.extend(result)
        return out

    @staticmethod
    def _chunks(texts: list[str]):
        current: list[str] = []
        chars = 0
        for text in texts:
            too_many = len(current) >= config.TRANSLATE_REQUEST_ITEMS
            too_big = chars + len(text) > config.TRANSLATE_REQUEST_CHARS
            if current and (too_many or too_big):
                yield current
                current, chars = [], 0
            current.append(text)
            chars += len(text)
        if current:
            yield current

    def _post_batch(self, texts: list[str], flores: str) -> "list | None":
        payload = {"texts": texts, "target_lang": flores}
        try:
            data = self._post("/translate/batch", payload, config.TRANSLATE_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            print(f"[TRANSLATE] HTTP {exc.code} del servicio: {detail}")
            return None
        except Exception as exc:
            print(f"[TRANSLATE] servicio inalcanzable: {type(exc).__name__}: {exc}")
            return None
        return (data or {}).get("translations")

    @staticmethod
    def _request(path: str, payload: "dict | None", timeout: int):
        url = f"{config.TRANSLATOR_URL.rstrip('/')}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path: str, timeout: int):
        return self._request(path, None, timeout)

    def _post(self, path: str, payload: dict, timeout: int):
        return self._request(path, payload, timeout)

    # --- unit collection --------------------------------------------------

    def _collect_leaf_blocks(self, soup) -> list:
        """Block elements that hold prose and contain no nested block.

        "Block" means "not inline", which is why INLINE_TAGS is the list that
        gets maintained. A <div> of inline content is a block here; under the
        old hand-written block list it was not, and its text nodes ended up
        being translated one at a time as blind fragments.
        """
        blocks = []
        for element in soup.find_all(True):
            name = (element.name or "").lower()
            if is_inline_name(name) or name in PROTECTED_TAGS:
                continue
            if self._has_protected_ancestor(element):
                continue
            if self._has_block_descendant(element):
                continue  # not a leaf: its children are the real units
            if not self._prose_text(element).strip():
                continue  # nothing but protected content and markup
            blocks.append(element)
        return blocks

    @staticmethod
    def _has_block_descendant(element) -> bool:
        for descendant in element.find_all(True):
            name = (descendant.name or "").lower()
            if not is_inline_name(name) and name not in PROTECTED_TAGS:
                return True
        return False

    def _prose_text(self, element) -> str:
        """Text of an element excluding anything inside protected tags."""
        parts = []
        for node in element.find_all(string=True):
            if node.parent is not None and self._has_protected_ancestor(node):
                continue
            parts.append(str(node))
        return "".join(parts)

    def _collect_orphan_text_nodes(self, soup, blocks: list) -> list:
        """Translatable text nodes that no collected block covers."""
        covered = set()
        for element in blocks:
            covered.add(id(element))
            for descendant in element.descendants:
                covered.add(id(descendant))

        collected = []
        for node in soup.find_all(string=True):
            if id(node) in covered:
                continue
            raw = str(node)
            core = raw.strip()
            if not core:
                continue
            if self._has_protected_ancestor(node):
                continue
            lead = raw[: len(raw) - len(raw.lstrip())]
            trail = raw[len(raw.rstrip()):]
            collected.append((node, lead, core, trail))
        return collected

    @staticmethod
    def _has_protected_ancestor(node) -> bool:
        for parent in node.parents:
            name = getattr(parent, "name", None)
            if name and name.lower() in PROTECTED_TAGS:
                return True
        return False

    # --- applying results -------------------------------------------------

    def _replace_contents(self, element, new_html: str):
        """Swap an element's children for a parsed translated fragment."""
        parsed = BeautifulSoup(new_html, "lxml")
        source = parsed.body if parsed.body is not None else parsed

        # Values are *inner* HTML. Unwrap any block tag the parser added around
        # them, otherwise we would nest <p> inside <p>: invalid markup that
        # breaks rendering and EPUB validation.
        for child in list(source.children):
            name = getattr(child, "name", None)
            if name and not is_inline_name(name) and name.lower() not in PROTECTED_TAGS:
                child.unwrap()

        children = list(source.contents)
        element.clear()
        for child in children:
            element.append(child.extract() if child.parent else child)


def _open_tag(tag) -> str:
    """Rebuild an opening tag with its attributes.

    Built from tag.attrs rather than sliced out of str(tag): slicing breaks
    whenever the inner text also appears inside an attribute value. The
    attributes come from the original element and are never sent to the model,
    so an href cannot come back corrupted.
    """
    parts = [tag.name]
    for key, value in tag.attrs.items():
        if value is None:
            parts.append(key)
            continue
        if isinstance(value, (list, tuple)):
            value = " ".join(str(item) for item in value)
        parts.append(f'{key}="{escape(str(value), quote=True)}"')
    return "<" + " ".join(parts) + ">"

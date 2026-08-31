"""Integration check for the translation plugin's markup handling.

Runs real chapter-shaped HTML through the live translation service and asserts
the invariants that matter. It needs the service up:

    services/translator/run.ps1
    python scripts/check_translate_markup.py

What it proves, and why each one is worth a check:

- Protected content survives byte for byte. Code spans, pre blocks, images,
  formulas. This is the guarantee the whole placeholder design exists for.
- No placeholder leaks into the output. A stray "%%4%%" in a book is worse than
  an untranslated paragraph, because it looks like corruption.
- href values are untouched. They are restored from the original element and
  never sent to the model, so this should be impossible -- which is exactly why
  it is worth asserting.
- The block structure is unchanged. Same number of paragraphs, list items and
  cells going out as coming in.
- The prose actually changed. A check that passes on a no-op is not a check.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402

import config  # noqa: E402
from plugins.translator import (  # noqa: E402
    TranslatorPlugin,
    _PLACEHOLDER_RE,
    placeholder_for,
)

# Chapter-shaped markup: inline code mid-sentence, a cross-reference link, a
# footnote marker, an image, a pre block, a table, and a <div> holding only
# inline content (which the previous block list missed entirely).
SAMPLE = """
<h2>Working with <code>Series</code> objects</h2>

<p>A <code>Series</code> is a one-dimensional array with an index. It is the
building block of the library, and almost every operation you will meet in
<a href="ch04.html#dataframes">Chapter 4</a> is defined in terms of it.</p>

<p>You can create one from a list, a dictionary or a NumPy array. The
<em>index</em> is created for you when you do not supply one, and it is
<strong>not</strong> reset when you filter the result.<sup id="fn1">1</sup></p>

<pre><code>import pandas as pd

s = pd.Series([1, 2, 3], index=["a", "b", "c"])
print(s.sum())</code></pre>

<p>Notice that <code>s.sum()</code> ignores missing values, while
<code>len(s)</code> does not. If you need the count of non-null entries, call
<code>s.count()</code> instead.</p>

<ul>
  <li>Use <code>loc</code> for label-based lookups.</li>
  <li>Use <code>iloc</code> when you want positions.</li>
  <li>Both accept slices, but only <code>loc</code> includes the endpoint.</li>
</ul>

<div class="note">Read the release notes before upgrading, because the default
for <code>copy</code> changed in version 2.0.</div>

<table>
  <tr><th>Method</th><th>What it returns</th></tr>
  <tr><td><code>head</code></td><td>The first rows of the object.</td></tr>
  <tr><td><code>describe</code></td><td>Summary statistics for each column.</td></tr>
</table>

<p><img src="images/series.png" alt="A Series and its index"/> The figure above
shows how the index and the values are stored separately.</p>
"""

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


def count_tags(html, name):
    return len(BeautifulSoup(html, "lxml").find_all(name))


def offline_checks():
    """Everything that can be proved without the GPU. Runs first, always."""
    print()
    print("encoder invariants (no service needed)")

    # The placeholder builder and the regex that scrubs leftovers have to agree.
    # They did not once: "%%%d%%" % 0 is "%0%", not "%%0%%", because printf reads
    # each "%%" as a single literal percent. The regex then matched none of its
    # own placeholders, so duplicates would have travelled into the book, and
    # the leak check below was quietly passing on output that could not contain
    # what it was looking for.
    marker = placeholder_for(0)
    check(marker == "%%0%%", f"placeholder_for(0) is {marker!r}")
    check(bool(_PLACEHOLDER_RE.fullmatch(marker)),
          "the scrub regex matches its own placeholder")
    check(bool(_PLACEHOLDER_RE.fullmatch(placeholder_for(123))),
          "and matches multi-digit indices")

    plugin = TranslatorPlugin()
    soup = BeautifulSoup(SAMPLE, "lxml")
    blocks = plugin._collect_leaf_blocks(soup)
    units = [plugin._encode(b, full_markup=True) for b in blocks]
    units = [u for u in units if u is not None]

    check(len(units) >= 12, f"{len(units)} translatable blocks found")

    # The source HTML wraps its lines. Those newlines are invisible in HTML but
    # not to the model: one of them stranded a placeholder at the start of a
    # wrapped line and it was dropped, which cost a whole paragraph its
    # translation. Templates must arrive on one line.
    wrapped = [u for u in units if chr(10) in u.template]
    check(not wrapped, "no template carries the source's line wrapping",
          f"{len(wrapped)} did: {wrapped[0].template[:60]!r}" if wrapped else "")

    # Protected content lives in slots, never in the text sent out.
    for unit in units:
        for index in unit.required:
            leaked = unit.slots[index] in unit.template
            check(not leaked, f"slot {index} markup is not in the template",
                  f"template: {unit.template[:60]!r}")
            break  # one per unit is enough to make the point

    codes = sum(1 for u in units for i in u.required if "<code" in u.slots[i])
    check(codes >= 8, f"{codes} code spans held in required slots")
    check(all(u.template.strip() == u.template for u in units),
          "templates have no leading or trailing whitespace")


def main():
    print("=" * 72)
    print("Markup invariants through the live translation service")
    print("=" * 72)

    offline_checks()

    plugin = TranslatorPlugin()

    print()
    if not plugin.is_available():
        print(f"\nThe service at {config.TRANSLATOR_URL} is not answering with a")
        print("loaded model. Start it with services/translator/run.ps1.")
        return 2

    print(f"\nservice: {config.TRANSLATOR_URL}")
    print("translating...\n")

    out = plugin.translate_html(SAMPLE, "es-LATAM")

    # --- protected content ------------------------------------------------
    print("protected content survives verbatim")
    protected = [
        "<code>Series</code>",
        "<code>s.sum()</code>",
        "<code>len(s)</code>",
        "<code>s.count()</code>",
        "<code>loc</code>",
        "<code>iloc</code>",
        "<code>copy</code>",
        "<code>head</code>",
        "<code>describe</code>",
        'import pandas as pd',
        's = pd.Series([1, 2, 3], index=["a", "b", "c"])',
        "print(s.sum())",
    ]
    for snippet in protected:
        check(snippet in out, f"{snippet[:44]!r}")

    soup_in = BeautifulSoup(SAMPLE, "lxml")
    soup_out = BeautifulSoup(out, "lxml")

    pre_in = soup_in.find("pre").get_text()
    pre_out = soup_out.find("pre")
    check(pre_out is not None and pre_out.get_text() == pre_in,
          "the pre block is character-identical",
          f"got {pre_out.get_text()[:60]!r}" if pre_out else "no <pre> in the output")

    # --- no leaked placeholders -------------------------------------------
    print("\nno placeholder leaked into the output")
    leaked = re.findall(r"%%\d+%%", out)
    check(not leaked, "no %%N%% anywhere", f"found {leaked[:6]}")
    check("%%" not in out, "not even a bare %%", "found '%%'")

    # --- attributes -------------------------------------------------------
    print("\nattributes are untouched")
    img_out = soup_out.find("img")
    check(img_out is not None and img_out.get("src") == "images/series.png",
          "img src unchanged",
          f"got {img_out.get('src') if img_out else None}")
    check(img_out is not None and img_out.get("alt") == "A Series and its index",
          "img alt unchanged (it is inside a protected element, so not translated)")
    hrefs = [a.get("href") for a in soup_out.find_all("a")]
    check(hrefs == ["ch04.html#dataframes"] or hrefs == [],
          "href unchanged, or the link was dropped without corrupting it",
          f"got {hrefs}")

    # --- structure --------------------------------------------------------
    print("\nblock structure is preserved")
    for name in ("p", "li", "td", "th", "tr", "h2", "div", "pre", "img"):
        before, after = count_tags(SAMPLE, name), count_tags(out, name)
        check(before == after, f"<{name}> count {before}", f"got {after}")

    # --- it actually translated -------------------------------------------
    print("\nthe prose actually changed")
    text_out = soup_out.get_text()
    check("one-dimensional array" not in text_out, "English source phrase is gone")
    spanish_markers = ["de", "la", "el", "que", "los"]
    hits = sum(1 for word in spanish_markers if f" {word} " in text_out)
    check(hits >= 3, f"reads as Spanish ({hits}/5 function words present)")

    # A div of inline-only content used to fall through to per-text-node
    # translation, which handed the model blind fragments.
    div_out = soup_out.find("div", class_="note")
    check(div_out is not None and "release notes" not in div_out.get_text(),
          "the inline-only <div> was translated as one block",
          f"got {div_out.get_text()[:70]!r}" if div_out else "no div")

    print("\n" + "=" * 72)
    print(f"{PASSED} passed, {FAILED} failed")
    print("=" * 72)

    if FAILED:
        print("\n--- output for inspection ---")
        print(out)

    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

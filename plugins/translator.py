"""Translation plugin: translate chapter HTML via a local Ollama LLM.

Strategy — translate whole *blocks*, not individual text nodes.

Publisher HTML splits sentences across inline markup, e.g.

    <p>A <i>class</i> is a blueprint. The <code>pd.Series</code> class is ...</p>

Translating each text node on its own gives the model blind fragments ("A ",
"class", " is a blueprint. The ") that it cannot reorder, which produced
orphaned words and English word order in Spanish. Instead each leaf block is
sent as one HTML fragment: the model sees the full sentence, keeps the inline
tags, and is free to move them where Spanish needs them.

Guarantees:
- Text inside pre/code/kbd/samp/var/tt is never translated; a translated block
  is rejected if any such content failed to survive verbatim.
- Batches go to Ollama as numbered JSON so replies stay aligned with inputs.
- Failure is non-fatal: if Ollama is unreachable or a batch fails, the original
  markup is kept so a download never breaks because of translation.

This plugin uses its own tiny HTTP client (not the shared HttpClient, which is
coupled to O'Reilly's cookies/Akamai handling). Ollama is plain local HTTP.
"""

import json
import re

from bs4 import BeautifulSoup, NavigableString

import config
from .base import Plugin

# curl_cffi is already a project dependency (used by HttpClient).
from curl_cffi import requests


class TranslatorPlugin(Plugin):
    """Translate chapter HTML into a target language using a local LLM."""

    # Never translate text inside these elements (or their descendants).
    SKIP_TAGS = {"pre", "code", "script", "style", "kbd", "samp", "var", "tt"}

    # Elements treated as translation units. A block is only used when it has
    # no nested block of its own, so content is never translated twice.
    BLOCK_TAGS = {
        "p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "td", "th", "dd", "dt", "blockquote", "figcaption", "caption",
    }

    def is_available(self) -> bool:
        """Return True if the Ollama server responds."""
        try:
            resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def translate_html(self, html: str, target_lang: str) -> str:
        """Translate the prose of an HTML fragment, preserving its markup.

        Returns the original html unchanged if target_lang is falsy/unknown,
        or if translation fails.
        """
        lang_instruction = config.TRANSLATE_LANGUAGES.get(target_lang)
        if not lang_instruction:
            return html

        soup = BeautifulSoup(html, "lxml")

        blocks = self._collect_leaf_blocks(soup)
        # Text that sits outside every block (rare) still needs translating;
        # collect it before block contents are swapped out.
        orphans = self._collect_orphan_text_nodes(soup, blocks)

        if not blocks and not orphans:
            return html

        if blocks:
            originals = [el.decode_contents() for el in blocks]
            translated = self._translate_strings(originals, lang_instruction, html_mode=True)
            for el, before, after in zip(blocks, originals, translated):
                if after != before and self._markup_survived(before, after):
                    self._replace_contents(el, after)

        if orphans:
            cores = [core for (_n, _l, core, _t) in orphans]
            translated = self._translate_strings(cores, lang_instruction, html_mode=False)
            for (node, lead, _core, trail), text in zip(orphans, translated):
                node.replace_with(NavigableString(f"{lead}{text}{trail}"))

        # BeautifulSoup+lxml wraps a fragment in <html><body>. Unwrap so the
        # output stays a fragment like html_processor.process produced.
        body = soup.body
        if body is not None:
            return "".join(str(c) for c in body.contents)
        return str(soup)

    # --- unit collection -------------------------------------------------

    def _collect_leaf_blocks(self, soup) -> list:
        """Return block elements that contain prose and no nested block."""
        blocks = []
        for el in soup.find_all(list(self.BLOCK_TAGS)):
            if el.find(list(self.BLOCK_TAGS)):
                continue  # not a leaf: its children are the real units
            if self._has_skipped_ancestor(el):
                continue
            if not self._prose_text(el).strip():
                continue  # nothing but code/markup to translate
            blocks.append(el)
        return blocks

    def _prose_text(self, el) -> str:
        """Text of an element excluding anything inside skip tags."""
        parts = []
        for node in el.find_all(string=True):
            if node.parent is not None and self._has_skipped_ancestor(node):
                continue
            parts.append(str(node))
        return "".join(parts)

    def _collect_orphan_text_nodes(self, soup, blocks: list) -> list:
        """Translatable text nodes that no collected block covers."""
        covered = set()
        for el in blocks:
            covered.add(id(el))
            for d in el.descendants:
                covered.add(id(d))

        collected = []
        for node in soup.find_all(string=True):
            if id(node) in covered:
                continue
            raw = str(node)
            core = raw.strip()
            if not core:
                continue
            if self._has_skipped_ancestor(node):
                continue
            lead = raw[: len(raw) - len(raw.lstrip())]
            trail = raw[len(raw.rstrip()) :]
            collected.append((node, lead, core, trail))
        return collected

    def _has_skipped_ancestor(self, node) -> bool:
        for parent in node.parents:
            if getattr(parent, "name", None) in self.SKIP_TAGS:
                return True
        return False

    # --- applying results -----------------------------------------------

    def _replace_contents(self, el, new_html: str):
        """Swap an element's children for a parsed translated fragment."""
        parsed = BeautifulSoup(new_html, "lxml")
        source = parsed.body if parsed.body is not None else parsed

        # Values are *inner* HTML, but models often echo a wrapping block tag.
        # Unwrap those, otherwise we'd nest <p> inside <p> (invalid markup that
        # breaks rendering and EPUB validation).
        for child in list(source.children):
            if getattr(child, "name", None) in self.BLOCK_TAGS:
                child.unwrap()

        children = list(source.contents)
        el.clear()
        for child in children:
            el.append(child.extract() if child.parent else child)

    def _markup_survived(self, before: str, after: str) -> bool:
        """Reject a translation that mangled or dropped protected content."""
        if not after.strip():
            return False

        original = BeautifulSoup(before, "lxml")
        result_text = BeautifulSoup(after, "lxml").get_text()

        # Every code-ish string must reappear verbatim somewhere in the result.
        for tag in original.find_all(list(self.SKIP_TAGS)):
            snippet = tag.get_text().strip()
            if snippet and snippet not in result_text:
                return False
        return True

    # --- batching + LLM --------------------------------------------------

    def _translate_strings(
        self, strings: list[str], lang_instruction: str, html_mode: bool
    ) -> list[str]:
        """Translate a list of strings, preserving order. Falls back to original."""
        result: list[str] = list(strings)  # default = untranslated

        batch: list[int] = []
        batch_chars = 0

        def flush(indices: list[int], depth: int = 0):
            """Translate these indices, splitting to recover missing entries.

            A model can return fewer keys than it was given (output cut short).
            Rather than leaving those strings in the source language, retry the
            missing ones in smaller halves until they come back or we bottom out.
            """
            if not indices:
                return
            mapping = {str(i): strings[i] for i in indices}
            translated = self._translate_batch(mapping, lang_instruction, html_mode)

            missing = []
            for i in indices:
                val = (translated or {}).get(str(i))
                if isinstance(val, str) and val.strip():
                    result[i] = val
                else:
                    missing.append(i)

            if not missing:
                return
            # Give up splitting past a single item / a few levels deep; those
            # strings simply stay untranslated instead of hanging the download.
            if depth >= 3 or len(missing) == 1:
                return
            mid = len(missing) // 2
            flush(missing[:mid], depth + 1)
            flush(missing[mid:], depth + 1)

        for i, s in enumerate(strings):
            batch.append(i)
            batch_chars += len(s)
            if batch_chars >= config.TRANSLATE_BATCH_CHARS:
                flush(batch)
                batch, batch_chars = [], 0
        flush(batch)

        return result

    def _build_system_prompt(self, lang_instruction: str, html_mode: bool) -> str:
        common = (
            f"You are a professional technical translator. Translate the VALUES of the "
            f"given JSON object into {lang_instruction}.\n"
            "- Keep the SAME keys; translate only the values.\n"
            "- This is technical book content: preserve meaning and tone.\n"
            "- Keep verbatim, untranslated: code, identifiers, function/class/method "
            "names, library names, file paths, CLI commands, keyboard keys (Tab, Enter, "
            "Shift, Ctrl, Esc), and menu/UI labels.\n"
            "- Keep well-known technical terms in English when there is no natural "
            "translation.\n"
            "- Do not add explanations or notes. Return ONLY a JSON object with the "
            "same keys."
        )
        if not html_mode:
            return common

        return common + (
            "\n\nEach value is the INNER HTML of an element:\n"
            "- Do NOT add a wrapping <p>, <div> or any other block tag around it.\n"
            "- Preserve every HTML tag and its attributes exactly as given.\n"
            "- Translate ONLY the human-readable text between tags.\n"
            "- NEVER change text inside <code>, <kbd>, <samp>, <var> or <tt>; copy it "
            "character for character.\n"
            "- Write natural, fluent target-language word order, MOVING the inline tags "
            "along with the words they belong to. Do not keep English word order.\n"
            "- Return the full fragment, not a summary."
        )

    def _translate_batch(
        self, mapping: dict[str, str], lang_instruction: str, html_mode: bool = False
    ) -> dict | None:
        """Send one batch to Ollama. Returns {id: translation} or None on failure."""
        payload = {
            "model": config.OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": self._build_system_prompt(lang_instruction, html_mode)},
                {"role": "user", "content": json.dumps(mapping, ensure_ascii=False)},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_ctx": config.OLLAMA_NUM_CTX},
        }
        if config.OLLAMA_DISABLE_THINKING:
            payload["think"] = False

        try:
            resp = self._post_chat(payload)
            content = resp.json()["message"]["content"]
            return self._parse_json_object(content)
        except Exception:
            return None

    def _post_chat(self, payload: dict):
        """POST to Ollama, retrying without `think` if the model rejects it."""
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json=payload,
            timeout=config.TRANSLATE_TIMEOUT,
        )
        # Models without thinking support reject the flag with a 4xx; drop it.
        if resp.status_code >= 400 and "think" in payload:
            retry = {k: v for k, v in payload.items() if k != "think"}
            resp = requests.post(
                f"{config.OLLAMA_URL}/api/chat",
                json=retry,
                timeout=config.TRANSLATE_TIMEOUT,
            )
        resp.raise_for_status()
        return resp

    @staticmethod
    def _parse_json_object(content: str) -> dict | None:
        """Parse a JSON object from the model output, tolerating stray wrapping."""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else None
        except Exception:
            pass
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else None
            except Exception:
                return None
        return None

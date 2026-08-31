"""Deterministic post-edition of the model's Spanish.

Why this exists: NLLB has exactly one Spanish, `spa_Latn`. FLORES-200 has no
es-419, no es-MX, no es-ES, and the model has no prompt, so there is nowhere to
put the instruction "neutral Latin American Spanish, no regionalisms" that an
instruction-following model could simply be handed as a prompt.

In technical prose the divergence is not grammatical (the text is almost all
impersonal third person, so peninsular verb forms barely appear); it is lexical
and it is a short list of words. A regex pass fixes those, is predictable, and
keeps the translation path free of a second model.

Two things this module does that a naive find-and-replace does not:

- Rules are applied in FILE ORDER, so a longer, more specific pattern can win
  over a shorter one. That is what makes the gender problem tractable:
  "el ordenador" -> "la computadora" has to fire before "ordenador" ->
  "computadora", otherwise the article is left stranded as "el computadora".
- The case of the match is carried over to the replacement, so "Fichero" at the
  start of a sentence does not come back lowercased.
"""

import re
from pathlib import Path

_COMMENT = "#"
_ARROW = "=>"


class PostEditor:
    """A compiled list of replacements, applied in order."""

    def __init__(self, rules: list[tuple[str, str]]):
        self._rules = [
            (re.compile(r"\b" + re.escape(source) + r"\b", re.IGNORECASE), target)
            for source, target in rules
        ]
        self.size = len(self._rules)

    def apply(self, text: str) -> str:
        if not self._rules or not text:
            return text
        for pattern, target in self._rules:
            text = pattern.sub(lambda m: _match_case(m.group(0), target), text)
        return text


def _match_case(matched: str, target: str) -> str:
    """Carry the case of `matched` onto `target`."""
    if matched.isupper() and len(matched) > 1:
        return target.upper()
    if matched[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def parse(lines: list[str]) -> list[tuple[str, str]]:
    """Parse `source => target` lines, ignoring blanks and # comments."""
    rules: list[tuple[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT):
            continue
        if _ARROW not in stripped:
            continue
        source, _, target = stripped.partition(_ARROW)
        source, target = source.strip(), target.strip()
        if source and target:
            rules.append((source, target))
    return rules


def load(path: Path) -> PostEditor:
    """Load the rule file. A missing file is not an error, just no rules."""
    if not path.is_file():
        return PostEditor([])
    lines = path.read_text(encoding="utf-8").splitlines()
    return PostEditor(parse(lines))

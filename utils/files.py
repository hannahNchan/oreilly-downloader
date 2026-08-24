"""File system utilities."""

import re
from pathlib import Path


def write_text_utf8(path: Path, content: str) -> None:
    """Write text as UTF-8 regardless of the platform default encoding.

    Path.write_text() without `encoding` falls back to the locale codec
    (cp1252 on Windows). That raises UnicodeEncodeError on characters such
    as U+21D0 and, worse, silently mangles accented text when it does not
    crash — even though the XML/EPUB headers we emit declare utf-8.

    The fallback only matters for input UTF-8 itself cannot represent (lone
    surrogates in upstream data): the character is kept as a numeric XML
    entity instead of losing the whole file.
    """
    try:
        path.write_text(content, encoding="utf-8")
    except UnicodeEncodeError:
        with open(path, "w", encoding="utf-8", errors="xmlcharrefreplace") as handle:
            handle.write(content)


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a cross-platform filename."""
    name = name.replace("/", "-").replace("\\", "-")
    name = name.replace(":", " -").replace("?", "").replace("*", "")
    name = name.replace('"', "'").replace("<", "").replace(">", "")
    name = name.replace("|", "-")
    name = name.strip().strip(".")
    if len(name) > 200:
        name = name[:200].strip()
    return name


def slugify(name: str) -> str:
    """Convert a string to a URL-friendly slug for folder names."""
    name = name.lower()
    name = re.sub(r"['\"]", "", name)
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    if len(name) > 100:
        name = name[:100].rstrip("-")
    return name

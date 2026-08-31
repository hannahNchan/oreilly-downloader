"""Find the same book in another language on O'Reilly.

There is no join key between editions. O'Reilly gives the Spanish edition of a
book its own ISBN and does not link it back to the English one, so the only way
to pair them is to search and judge the results.

What makes that tractable is the authors. A translated title shares few words
with the original -- "Programming TypeScript" against "Programacion TypeScript"
is lucky, most are not -- but the author list is identical across editions.
So the author is the search key and the title is the tie-breaker, not the other
way round.

This is a guess with a score attached, never a fact. The score travels to the UI
so the matched title can be shown next to the checkbox: a bundle built from the
wrong edition is worse than no bundle, and only the person looking at the two
titles can tell.
"""

import re
import unicodedata
from difflib import SequenceMatcher

from .base import Plugin

# Below this, the counterpart is not offered at all.
MIN_SCORE = 0.55
# At or above this, it is safe enough to preselect.
CONFIDENT_SCORE = 0.75
# O'Reilly does not always carry an author list. Measured: several Spanish
# TypeScript titles come back with none at all. Without authors the score could
# never clear MIN_SCORE, so those books could never match anything -- the title
# has to carry it alone, and the bar for it rises accordingly.
TITLE_ONLY_MIN_TITLE = 0.75

# Edition noise that differs between printings and would otherwise drag the
# title similarity down.
_EDITION_NOISE = re.compile(
    r"\b("
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|primera|segunda|tercera|cuarta|quinta|sexta|septima|octava"
    r"|edition|edicion|ed|revised|updated|spanish|english|espanol|ingles"
    r"|version|vol|volume|volumen"
    r")\b",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, drop edition noise."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _EDITION_NOISE.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def _name_key(name: str) -> str:
    """A person's name reduced to something comparable across editions.

    Sorted tokens, because catalogues disagree on "Meeks, Elijah" against
    "Elijah Meeks", and initials get dropped because one edition writes
    "Anne-Marie" where another writes "A.-M.".
    """
    tokens = [t for t in normalize(name).split() if len(t) > 1]
    return " ".join(sorted(tokens))


def author_overlap(left: list, right: list) -> float:
    """Fraction of the smaller author list that appears in the other."""
    a = {_name_key(x) for x in (left or []) if _name_key(x)}
    b = {_name_key(x) for x in (right or []) if _name_key(x)}
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


class EditionsPlugin(Plugin):
    """Look up a book's counterpart in another language."""

    def __init__(self):
        # One search per book is enough, and this runs while a modal is opening.
        self._cache: dict[tuple, dict] = {}

    # --- public ----------------------------------------------------------

    def counterpart(self, book_id: str, target_language: str = "es",
                    info: dict | None = None) -> dict:
        """Return the best candidate in `target_language`, with its score.

        Shape:
            {"found": bool, "confident": bool, "score": float,
             "candidate": {...} | None, "candidates": [...],
             "source": {...}, "reason": str}
        """
        key = (str(book_id), target_language)
        if key in self._cache:
            return self._cache[key]

        result = self._lookup(book_id, target_language, info)
        self._cache[key] = result
        return result

    def clear_cache(self) -> None:
        self._cache.clear()

    # --- internals -------------------------------------------------------

    def _lookup(self, book_id: str, target_language: str, info: dict | None) -> dict:
        book = self.kernel["book"]

        if info is None:
            try:
                info = book.fetch(book_id) or {}
            except Exception as exc:
                return self._empty(f"no se pudo leer el libro: {exc}")

        title = (info.get("title") or "").strip()
        authors = info.get("authors") or []
        source_language = (info.get("language") or "en").split("-")[0].lower()

        if source_language == target_language:
            return self._empty(
                f"el libro ya esta en '{target_language}'",
                source=self._brief(info, book_id),
            )
        if not title and not authors:
            return self._empty("el libro no tiene titulo ni autores para buscar")

        candidates = self._gather(book, title, authors, target_language)
        candidates = [c for c in candidates if str(c.get("id")) != str(book_id)]

        if not candidates:
            return self._empty(
                f"no hay ediciones en '{target_language}' para este titulo o autor",
                source=self._brief(info, book_id),
            )

        scored = []
        for item in candidates:
            score, parts = self._score(item, title, authors)
            entry = self._brief(item, item.get("id"))
            entry["score"] = round(score, 3)
            entry["match"] = parts
            scored.append(entry)

        scored.sort(key=lambda c: c["score"], reverse=True)
        best = scored[0]

        if best["score"] < MIN_SCORE:
            return self._empty(
                f"el mejor candidato no se parece lo suficiente ({best['score']:.2f})",
                source=self._brief(info, book_id),
                candidates=scored[:5],
            )

        same_title = bool(best.get("match", {}).get("same_title"))
        warning = ""
        if same_title:
            warning = ("titulo identico al original: puede ser la misma obra "
                       "listada dos veces, no una traduccion")

        return {
            "found": True,
            "confident": best["score"] >= CONFIDENT_SCORE and not same_title,
            "warning": warning,
            "score": best["score"],
            "candidate": best,
            "candidates": scored[:5],
            "source": self._brief(info, book_id),
            "reason": "",
        }

    def _gather(self, book, title: str, authors: list, language: str) -> list:
        """Search by author first, then by title, and merge by id.

        Author first because it survives translation. Title still runs: some
        catalogues list a translated edition under a different author spelling,
        and a near-identical title then rescues the match.
        """
        found: dict[str, dict] = {}

        queries = []
        if authors:
            queries.append(str(authors[0]))
        if title:
            queries.append(title)

        for query in queries:
            try:
                payload = book.search(query, limit=25, language=language)
            except Exception:
                continue
            for item in (payload or {}).get("results", []) or []:
                item_id = str(item.get("id") or "")
                if item_id and item_id not in found:
                    found[item_id] = item

        return list(found.values())

    @staticmethod
    def _score(item: dict, title: str, authors: list) -> tuple[float, dict]:
        """Weighted score. Authors dominate; the title breaks ties.

        The weights are not tuned, they are ordered: an identical author list
        with an unrecognisable title is a likely translation, while a similar
        title by different people is a different book.
        """
        item_authors = item.get("authors") or []
        by_author = author_overlap(authors, item_authors)
        by_title = title_similarity(title, item.get("title") or "")

        if not authors or not item_authors:
            # Nothing to compare people with. The title is all there is, so it
            # has to be strong on its own or the candidate is dropped outright.
            mode = "title_only"
            score = 0.85 * by_title if by_title >= TITLE_ONLY_MIN_TITLE else 0.0
        else:
            mode = "author"
            score = 0.65 * by_author + 0.30 * by_title

        # An identical title with an identical author list is not a
        # translation, it is almost always the same book listed twice under
        # two language tags. A real translation keeps the authors and CHANGES
        # the title. The exception is a title that does not translate at all
        # (a product name), so this does not reject -- it refuses to call the
        # match confident, and says why, and lets the person decide.
        same_title = normalize(title) == normalize(item.get("title") or "")

        return score, {
            "authors": round(by_author, 3),
            "title": round(by_title, 3),
            "mode": mode,
            "same_title": same_title,
        }

    @staticmethod
    def _brief(item: dict, book_id) -> dict:
        return {
            "id": str(book_id or item.get("id") or ""),
            "title": item.get("title") or "",
            "authors": item.get("authors") or [],
            "publishers": item.get("publishers") or [],
            "language": item.get("language") or "",
            "cover": item.get("cover") or item.get("cover_url") or "",
            "issued": item.get("issued") or "",
        }

    @staticmethod
    def _empty(reason: str, source: dict | None = None,
               candidates: list | None = None) -> dict:
        return {
            "found": False,
            "confident": False,
            "warning": "",
            "score": 0.0,
            "candidate": None,
            "candidates": candidates or [],
            "source": source,
            "reason": reason,
        }

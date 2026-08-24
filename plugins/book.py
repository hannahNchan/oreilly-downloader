import re
from urllib.parse import quote_plus

import config

from .base import Plugin


_COVER_WIDTH_RE = re.compile(r'/\d+w/?$')
_HIGH_RES_COVER_WIDTH = '1200w'


def _upgrade_cover_url(url: str) -> str:
    """Upgrade an O'Reilly cover URL to a high-resolution variant.

    O'Reilly serves covers at /library/cover/{isbn}/ and /covers/urn:orm:book:{id}/,
    optionally with a /{N}w/ width segment. The base URL (no width) returns a
    low-res thumbnail (~160x184). Appending /1200w/ returns a print-quality image.
    """
    if "/library/cover/" not in url and "/covers/urn:orm:book:" not in url:
        return url
    # Strip any existing width segment from the end, then append high-res width.
    stripped = _COVER_WIDTH_RE.sub("", url, count=1).rstrip("/")
    return f"{stripped}/{_HIGH_RES_COVER_WIDTH}/"


class BookPlugin(Plugin):
    def fetch(self, book_id: str) -> dict:
        search_data = self._fetch_search(book_id)
        epub_data = self._fetch_epub(book_id)

        cover_url = search_data.get("cover_url")
        if cover_url:
            cover_url = _upgrade_cover_url(cover_url)

        return {
            "id": book_id,
            "ourn": epub_data.get("ourn", ""),
            "title": epub_data.get("title") or "Unknown",
            "authors": search_data.get("authors", []),
            "publishers": search_data.get("publishers", []),
            "description": (epub_data.get("descriptions") or {}).get("text/html", ""),
            "cover_url": cover_url,
            "isbn": epub_data.get("isbn", ""),
            "language": epub_data.get("language", "en"),
            "publication_date": epub_data.get("publication_date", ""),
            "virtual_pages": epub_data.get("virtual_pages"),
            "chapters_url": epub_data.get("chapters"),
            "toc_url": epub_data.get("table_of_contents"),
            "spine_url": epub_data.get("spine"),
            "files_url": epub_data.get("files"),
        }

    def _fetch_search(self, book_id: str) -> dict:
        url = f"{config.API_V2}/search/?query={book_id}&limit=1"
        data = self.http.get_json(url)
        results = data.get("results", [])
        if not results:
            return {}
        return results[0]

    def _fetch_epub(self, book_id: str) -> dict:
        url = f"{config.API_V2}/epubs/urn:orm:book:{book_id}/"
        return self.http.get_json(url)

    # Vocabulario de filtros que la API acepta de verdad (verificado contra
    # el endpoint: cualquier otro valor de `sort` devuelve HTTP 400).
    SORT_OPTIONS = ("relevance", "popularity", "publication_date", "title")

    # Idiomas con resultados reales. Se omitió "tr" (0 resultados).
    LANGUAGES = {
        "en": "Inglés", "es": "Español", "pt": "Portugués", "fr": "Francés",
        "de": "Alemán", "it": "Italiano", "ja": "Japonés", "zh": "Chino",
        "ko": "Coreano", "ru": "Ruso", "pl": "Polaco",
    }

    def search(
        self,
        query: str,
        limit: int = 25,
        page: int = 0,
        language: str | None = None,
        sort: str | None = None,
    ) -> dict:
        """Search books, one page at a time, filtering server-side.

        `formats=book` siempre va: esta app sólo descarga libros, así que traer
        video/audiobook sería traer resultados no descargables. Los filtros
        opcionales se OMITEN cuando vienen vacíos — mandar `languages=` podría
        filtrar a cero en lugar de traer todo.

        Returns the page plus the API's own totals so callers can paginate.
        `has_more` is what decides whether to keep going, not len(results).
        """
        params = [
            f"query={quote_plus(query)}",
            f"limit={limit}",
            f"page={page}",
            "formats=book",
        ]
        if language:
            params.append(f"languages={quote_plus(language)}")
        if sort and sort != "relevance":  # relevance es el default de la API
            params.append(f"sort={quote_plus(sort)}")

        url = f"{config.API_V2}/search/?" + "&".join(params)
        data = self.http.get_json(url)

        results = [
            {
                "id": item.get("archive_id"),
                "title": item.get("title"),
                "authors": item.get("authors", []),
                "cover_url": item.get("cover_url"),
                "publishers": item.get("publishers", []),
            }
            for item in data.get("results", [])
            # Red de seguridad: con formats=book no debería hacer falta,
            # pero si el parámetro dejara de aplicarse no queremos ofrecer
            # videos como si fueran libros descargables.
            if item.get("content_format") == "book"
        ]

        return {
            "results": results,
            "page": page,
            "limit": limit,
            "total": self._book_total(data),
            "total_all_formats": data.get("total", 0),
            "has_more": bool(data.get("next")),
        }

    @staticmethod
    def _book_total(data: dict) -> int:
        """Exact number of *books* matching, from the API's format facet.

        The facet arrives flat: ["article", 875, "book", 51645, ...]. Falling
        back to `total` would over-count, since that includes video/audio.
        """
        facet = ((data.get("facets") or {}).get("facet_fields") or {}).get("format") or []
        for i in range(0, len(facet) - 1, 2):
            if facet[i] == "book":
                return facet[i + 1]
        return data.get("total", 0)

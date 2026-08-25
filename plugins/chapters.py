import re
import time

from .base import Plugin
from .errors import SessionExpired
from core.types import ChapterInfo
import config


class ChaptersPlugin(Plugin):
    """Plugin for fetching book chapters and their content."""

    def fetch_list(self, book_id: str) -> list[ChapterInfo]:
        """Fetch list of chapters for a book."""
        url = f"{config.API_V2}/epub-chapters/?epub_identifier=urn:orm:book:{book_id}"
        chapters: list[ChapterInfo] = []

        while url:
            data = self.http.get_json(url)
            for ch in data.get("results", []):
                chapters.append(ChapterInfo(
                    ourn=ch.get("ourn", ""),
                    title=ch.get("title", ""),
                    filename=self._extract_filename(ch.get("reference_id", "")),
                    content_url=ch.get("content_url", ""),
                    images=(ch.get("related_assets") or {}).get("images", []),
                    stylesheets=(ch.get("related_assets") or {}).get("stylesheets", []),
                    virtual_pages=ch.get("virtual_pages"),
                    minutes_required=ch.get("minutes_required"),
                ))
            url = data.get("next")

        return chapters

    def fetch_toc(self, book_id: str) -> list[dict]:
        url = f"{config.API_V2}/epubs/urn:orm:book:{book_id}/table-of-contents/"
        data = self.http.get_json(url)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    _FRONT_MATTER_KEYWORDS = ("cover", "halftitle", "titlepage", "title-page", "contents", "toc")

    def reorder_by_toc(self, chapters: list[ChapterInfo], toc: list[dict]) -> list[ChapterInfo]:
        """Reorder chapters to match the TOC reading order."""
        ordered_filenames = self._flatten_toc_filenames(toc)
        if not ordered_filenames:
            return chapters

        chapter_map = {ch["filename"]: ch for ch in chapters}

        ordered = []
        for filename in ordered_filenames:
            if filename in chapter_map:
                ordered.append(chapter_map.pop(filename))

        # Split remaining into front matter (before TOC content) and back matter (after)
        front = []
        back = []
        for ch in chapters:
            if ch["filename"] not in chapter_map:
                continue
            if self._is_front_matter(ch):
                front.append(ch)
            else:
                back.append(ch)

        return front + ordered + back

    def _is_front_matter(self, ch: ChapterInfo) -> bool:
        filename_lower = ch["filename"].lower()
        title_lower = ch["title"].lower()
        for keyword in self._FRONT_MATTER_KEYWORDS:
            if keyword in filename_lower or keyword in title_lower:
                return True
        return False

    # A chapter served without a valid session comes back as HTTP 200 with a
    # short "abstract" preview whose text is cut off with an ellipsis, instead
    # of a 403. Silently accepting it produces a book with only the first
    # paragraph of each chapter, so detect it and retry/fail loudly.
    _PREVIEW_MAX_BYTES = 6000
    _PREVIEW_RETRIES = 3
    # Cuanto vale la comprobacion de sesion antes de repetirla. Un libro puede
    # tener varios capitulos cortos seguidos (apendices, indice) y no hace
    # falta un control por cada uno.
    _ALIVE_TTL = 60

    def __init__(self):
        # Un capitulo que llego COMPLETO sirve de patron de control: si vuelve
        # a llegar completo, la sesion funciona. Se guarda entre descargas a
        # proposito — cualquier capitulo completo prueba lo mismo.
        self._reference_url: str | None = None
        self._alive_until: float = 0.0

    def fetch_content(self, content_url: str) -> str:
        """Fetch chapter HTML, retrying if the server returns a preview stub."""
        last = ""
        for attempt in range(self._PREVIEW_RETRIES):
            last = self.http.get_text(content_url)
            if not self._looks_truncated(last):
                if len(last) > self._PREVIEW_MAX_BYTES:
                    self._reference_url = content_url
                return last

            # "Corto y acabado en puntos suspensivos" es una SOSPECHA, no un
            # veredicto: un capitulo de conclusion o un apendice pueden ser asi
            # de verdad. Antes se daba por caducada la sesion sin comprobarlo, y
            # entonces no habia cookies que arreglaran nada — pegabas unas
            # nuevas, reintentaba, y el mismo capitulo volvia a fallar igual.
            if self._session_alive():
                print(f"[CHAPTERS] {content_url} es corto y acaba en puntos "
                      f"suspensivos, pero la sesion responde completo: "
                      f"se acepta como contenido real ({len(last)} bytes)")
                return last

            if attempt < self._PREVIEW_RETRIES - 1:
                # The session may have gone stale mid-download; reload cookies
                # from disk (the user may have refreshed them) and back off.
                reload_cookies = getattr(self.http, "reload_cookies", None)
                if reload_cookies:
                    reload_cookies()
                time.sleep(config.RETRY_BACKOFF * (attempt + 1))

        # SessionExpired y no RuntimeError: la cola reacciona distinto a esto
        # que a cualquier otro fallo — en vez de perder el trabajo, lo pausa y
        # espera cookies nuevas para seguir donde iba.
        raise SessionExpired(
            f"O'Reilly devolvio solo un avance de {content_url} "
            f"({len(last)} bytes) y {self._jwt_note()} "
            "Pega cookies nuevas para continuar."
        )

    def _session_alive(self) -> bool:
        """True si un capitulo que ya llego completo sigue llegando completo.

        Es la unica prueba fiable de que la sesion sirve: mirar la fecha `exp`
        del orm-jwt no vale, porque O'Reilly puede dejar de honrar un token que
        aun no ha caducado (por ejemplo si el navegador lo rota en otra pestaña).
        Sin patron de control todavia no se puede afirmar nada, y ahi se
        mantiene la conducta prudente: tratarlo como sesion caida.
        """
        if not self._reference_url:
            return False
        if time.time() < self._alive_until:
            return True
        try:
            control = self.http.get_text(self._reference_url)
        except Exception:  # noqa: BLE001 - si el control falla, no se afirma nada
            return False
        if len(control) > self._PREVIEW_MAX_BYTES:
            self._alive_until = time.time() + self._ALIVE_TTL
            return True
        return False

    def _jwt_note(self) -> str:
        """Que decia el token en el momento del fallo.

        Sin esto el mensaje decia "la sesion caduco" incluso con un token
        valido, justo cuando la cabecera mostraba minutos por delante: dos
        afirmaciones contradictorias, y ninguna explicaba nada.
        """
        estado = getattr(self.http, "get_jwt_status", lambda: None)()
        if estado is None:
            return "no hay token orm-jwt guardado."
        if not estado.get("valid"):
            return "el token orm-jwt ya habia caducado."
        return ("el token orm-jwt aun era valido: lo que dejo de servir es la "
                "sesion, no el token (suele pasar si el navegador la renovo "
                "en otra pestaña).")

    def _looks_truncated(self, html: str) -> bool:
        """True if the HTML looks like O'Reilly's cut-off abstract preview."""
        if len(html) > self._PREVIEW_MAX_BYTES:
            return False
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        # Preview bodies end mid-sentence with "..." or the ellipsis character.
        return text.endswith("...") or text.endswith("…")

    def _extract_filename(self, reference_id: str) -> str:
        if "-/" in reference_id:
            return reference_id.split("-/")[1]
        return reference_id

    def _flatten_toc_filenames(self, toc_items: list[dict]) -> list[str]:
        """Extract ordered, deduplicated filenames from the TOC tree."""
        result: list[str] = []
        seen: set[str] = set()

        def walk(items: list[dict]):
            for item in items:
                ref_id = item.get("reference_id", "")
                if ref_id:
                    filename = self._extract_filename(ref_id)
                    if filename and filename not in seen:
                        result.append(filename)
                        seen.add(filename)
                children = item.get("children", [])
                if children:
                    walk(children)

        walk(toc_items)
        return result

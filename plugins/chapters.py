import re
import time

from .base import Plugin
from .errors import PreviewOnly, SessionExpired
from core.types import ChapterInfo
import config


# El urn del libro dentro de una url de capitulo. Sirve para preguntarle a la
# API por el indice de ESE libro sin tener que arrastrar el book_id hasta aqui.
_BOOK_URN_RE = re.compile(r"(urn:orm:book:[^/]+)")


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
    # Intentos COMPLETOS (con pausa y cookies nuevas en medio) antes de dejar de
    # culpar a la sesion. Dos: el primero puede ser la sesion de verdad, el
    # segundo ya demuestra que las cookies no eran el problema.
    _PREVIEW_GIVE_UP = 2
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
        # Cuantas veces ha fallado cada capitulo con un avance. Sobrevive a la
        # pausa porque este plugin es un singleton del kernel, y es justo lo que
        # rompe el bucle de cookies.
        self._preview_failures: dict[str, int] = {}
        # Contabilidad de la descarga en curso. Es lo que permite distinguir
        # "una portada diminuta" de "este libro entero son avances", que es
        # justo lo que no se podia decidir mirando una pagina de una en una.
        self.full_pages = 0
        self.short_accepted: list[tuple[str, int]] = []
        self.consecutive_short = 0

    def reset_stats(self) -> None:
        """Pone a cero la contabilidad. Una llamada por libro.

        El plugin es un singleton del kernel, asi que sin esto las cuentas de
        una descarga contaminarian el veredicto de la siguiente.
        """
        self.full_pages = 0
        self.short_accepted = []
        self.consecutive_short = 0

    def fetch_content(self, content_url: str) -> str:
        """Fetch chapter HTML, retrying if the server returns a preview stub."""
        last = ""
        for attempt in range(self._PREVIEW_RETRIES):
            last = self.http.get_text(content_url)
            if not self._looks_truncated(last):
                # Pagina sana: el detector no la marco, asi que es contenido
                # correcto SEA DEL TAMANO QUE SEA. Contarla solo si pasaba de
                # 6000 bytes era el error que mataba libros buenos: hay titulos
                # de O'Reilly partidos en secciones pequenas donde ninguna
                # pagina llega a ese tamano.
                self.full_pages += 1
                self.consecutive_short = 0
                # El umbral sigue mandando para UNA cosa: elegir un capitulo lo
                # bastante grande como patron de control. Una seccion de dos
                # parrafos no sirve para distinguir sesion viva de sesion caida.
                if len(last) > self._PREVIEW_MAX_BYTES:
                    self._reference_url = content_url
                self._preview_failures.pop(content_url, None)
                return last

            # "Corto y acabado en puntos suspensivos" es una SOSPECHA, no un
            # veredicto: un capitulo de conclusion o un apendice pueden ser asi
            # de verdad. Antes se daba por caducada la sesion sin comprobarlo, y
            # entonces no habia cookies que arreglaran nada — pegabas unas
            # nuevas, reintentaba, y el mismo capitulo volvia a fallar igual.
            if self._session_alive(content_url):
                # Se acepta, pero NO en silencio. La sesion responde, asi que no
                # tiene sentido culpar a las cookies; que la pagina sea contenido
                # real de verdad es otra pregunta, y esa la responde el recuento
                # global al final de la descarga.
                self.short_accepted.append((content_url, len(last)))
                self.consecutive_short += 1
                print(f"[CHAPTERS] {content_url} llego corto ({len(last)} bytes) "
                      f"y la sesion responde: se acepta provisionalmente "
                      f"({len(self.short_accepted)} cortas / "
                      f"{self.full_pages} completas)")
                return last

            if attempt < self._PREVIEW_RETRIES - 1:
                # The session may have gone stale mid-download; reload cookies
                # from disk (the user may have refreshed them) and back off.
                reload_cookies = getattr(self.http, "reload_cookies", None)
                if reload_cookies:
                    reload_cookies()
                time.sleep(config.RETRY_BACKOFF * (attempt + 1))

        fallos = self._preview_failures.get(content_url, 0) + 1
        self._preview_failures[content_url] = fallos

        # Segunda vuelta con este mismo capitulo: ya hubo una pausa y unas
        # cookies nuevas en medio, y sigue llegando un avance. La sesion no es
        # el problema, asi que se deja de pedir cookies -- pedirlas otra vez es
        # el bucle que esto viene a romper.
        if fallos >= self._PREVIEW_GIVE_UP:
            self._preview_failures.pop(content_url, None)
            raise PreviewOnly(
                f"O'Reilly sigue devolviendo solo un avance de {content_url} "
                f"({len(last)} bytes) despues de {fallos} rondas con cookies "
                "recargadas. La sesion no es el problema: o esta pagina es "
                "legitimamente corta, o este libro no esta completo en esta "
                "cuenta.",
                html=last,
            )

        # Primera vuelta: puede ser la sesion, asi que se pausa y se piden
        # cookies. Pero no se AFIRMA que haya caducado cuando no hay forma de
        # saberlo.
        #
        # SessionExpired y no RuntimeError: la cola reacciona distinto a esto
        # que a cualquier otro fallo — en vez de perder el trabajo, lo pausa y
        # espera cookies nuevas para seguir donde iba.
        if self._reference_url is None:
            detalle = ("Todavia no habia llegado ningun capitulo completo, asi "
                       "que no hay patron de control: puede ser la sesion, o "
                       "puede que este libro no este completo en esta cuenta.")
        else:
            detalle = ("Un capitulo que antes llegaba completo tampoco llega "
                       "ahora, asi que la sesion es lo mas probable.")

        raise SessionExpired(
            f"O'Reilly devolvio solo un avance de {content_url} "
            f"({len(last)} bytes) y {self._jwt_note()} {detalle} "
            "Pega cookies nuevas para continuar."
        )

    def seed_reference(self, content_url: str, size: int) -> None:
        """Adopta un capitulo ya completo (leido de cache) como patron de control.

        Reanudar una descarga lee los capitulos de disco sin pasar por
        fetch_content, asi que _reference_url se quedaba vacio y _session_alive
        no podia afirmar nada. Consecuencia medida: about_pearson.xhtml, que mide
        1140 bytes porque de verdad mide eso, pasaba por avance truncado y
        tumbaba la descarga entera de un libro que estaba perfectamente.
        """
        if self._reference_url is None and size > self._PREVIEW_MAX_BYTES:
            self._reference_url = content_url

    def _session_alive(self, content_url: str = "") -> bool:
        """True si la sesion de O'Reilly sigue sirviendo contenido.

        Mirar la fecha `exp` del orm-jwt no vale: O'Reilly puede dejar de honrar
        un token que aun no ha caducado (por ejemplo si el navegador lo rota en
        otra pestaña). Asi que se comprueba pidiendo algo de verdad.

        Dos vias, y la segunda es la que faltaba:

        1. Si hay patron de control -- un capitulo que ya llego completo --, se
           vuelve a pedir. Si sigue llegando completo, la sesion sirve.
        2. Si NO hay patron, se pregunta al indice del libro. Esto pasa SIEMPRE
           al principio de una descarga, y justo ahi las primeras paginas
           (portada, creditos, dedicatoria) son legitimamente diminutas: antes
           se devolvia False sin ninguna prueba y la descarga se paraba a pedir
           cookies que no arreglaban nada.
        """
        if time.time() < self._alive_until:
            return True

        if self._reference_url:
            try:
                control = self.http.get_text(self._reference_url)
            except Exception:  # noqa: BLE001 - si el control falla, no se afirma nada
                return False
            if len(control) > self._PREVIEW_MAX_BYTES:
                self._alive_until = time.time() + self._ALIVE_TTL
                return True
            return False

        return self._session_probe(content_url)

    def _session_probe(self, content_url: str) -> bool:
        """La sesion responde a un endpoint que exige auth?

        El indice del propio libro: lo acaba de pedir el downloader con exito
        segundos antes, exige sesion valida, y su tamaño no depende de ninguna
        pagina. Es la prueba que no existia cuando aun no hay patron de control.
        """
        match = _BOOK_URN_RE.search(content_url or "")
        if not match:
            return False
        url = f"{config.API_V2}/epubs/{match.group(1)}/table-of-contents/"
        try:
            data = self.http.get_json(url)
        except Exception:  # noqa: BLE001 - sin respuesta no se afirma nada
            return False
        if not data:
            return False
        self._alive_until = time.time() + self._ALIVE_TTL
        return True

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

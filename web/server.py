"""Web server for O'Reilly Ingest."""

import json
import re
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from core import Kernel, create_default_kernel
from plugins import ChunkConfig
import config


class DownloaderHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the downloader web interface."""

    kernel: Kernel = None
    # La cola de transferencia lleva su propio estado: puede correr con una
    # descarga en marcha y mezclarlos dejaria la UI mostrando cualquier cosa.
    transfer_progress: dict = {}
    _transfer_lock = threading.Lock()

    @classmethod
    def _set_transfer(cls, data: dict):
        with cls._transfer_lock:
            cls.transfer_progress = data

    @classmethod
    def _update_transfer(cls, **kwargs):
        with cls._transfer_lock:
            cls.transfer_progress.update(kwargs)

    def __init__(self, *args, **kwargs):
        self.static_dir = Path(__file__).parent / "static"
        super().__init__(*args, directory=str(self.static_dir), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # La conexion se reutiliza entre peticiones (keep-alive), asi que el
        # flag de "esto es media" hay que limpiarlo en cada una.
        self._media_response = False

        if path == "/api/status":
            self._handle_status()
        elif path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", params.get("query", [""]))[0]
            limit = self._int_param(params, "limit", default=25, low=1, high=100)
            page = self._int_param(params, "page", default=0, low=0, high=1000)
            language = (params.get("language", [""])[0] or "").strip()
            sort = (params.get("sort", [""])[0] or "").strip()
            content_type = (params.get("content_type", ["book"])[0] or "book").strip()
            self._handle_search(query, limit=limit, page=page, language=language,
                                sort=sort, content_type=content_type)
        elif match := re.match(r"/api/audiobook/([^/]+)/chapters$", path):
            self._handle_audiobook_chapters(match.group(1))
        elif match := re.match(r"/api/audiobook/([^/]+)$", path):
            self._handle_audiobook_info(match.group(1))
        elif match := re.match(r"/api/book/([^/]+)/chapters$", path):
            self._handle_chapters_list(match.group(1))
        elif match := re.match(r"/api/book/([^/]+)/editions$", path):
            language = (parse_qs(parsed.query).get("language", ["es"])[0] or "es").strip()
            self._handle_editions(match.group(1), language)
        elif match := re.match(r"/api/book/([^/]+)$", path):
            self._handle_book_info(match.group(1))
        elif path == "/api/progress":
            self._handle_progress()
        elif path == "/api/queue":
            self._send_json(self.kernel["queue"].snapshot())
        elif path == "/api/watchlist":
            self._send_json({"items": self.kernel["watchlist"].annotated()})
        elif path == "/api/settings":
            self._handle_get_settings()
        elif path == "/api/formats":
            self._handle_formats()
        elif path == "/api/search-filters":
            self._handle_search_filters()
        elif path == "/api/library":
            self._handle_library(parse_qs(parsed.query))
        elif path == "/api/library/transfer":
            with self._transfer_lock:
                self._send_json(dict(self.transfer_progress))
        elif match := re.match(r"/api/library/tracks/(.+)$", path):
            self._handle_library_tracks(unquote(match.group(1)))
        elif match := re.match(r"/api/library/audio/(.+)/(\d+)$", path):
            self._handle_library_audio(unquote(match.group(1)), int(match.group(2)))
        elif match := re.match(r"/api/library/file/(.+)/(epub|pdf)$", path):
            self._handle_library_file(unquote(match.group(1)), match.group(2))
        elif match := re.match(r"/api/library/cover/(.+)$", path):
            self._handle_library_cover(unquote(match.group(1)))
        else:
            super().do_GET()

    def send_response(self, code, message=None):
        """Tell browsers never to cache the static assets.

        Without this, an edited app.js/style.css keeps being served from the
        browser cache and the UI silently runs stale code after a change.

        Portadas y audio quedan fuera: ahi el no-store es contraproducente —
        cada vez que mueves la barra del reproductor el navegador pide otro
        rango, y sin cache se vuelve a traer el archivo entero.
        """
        super().send_response(code, message)
        if not getattr(self, "_media_response", False):
            self.send_header("Cache-Control", "no-store, must-revalidate")

    def do_OPTIONS(self):
        """Answer CORS preflight so browser clients can POST cookies/JSON."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(body) if body else {}

        if self.path == "/api/download":
            self._handle_download(data)
        elif self.path == "/api/cookies":
            self._handle_cookies(data)
        elif self.path == "/api/cancel":
            self._handle_cancel()
        elif self.path == "/api/reveal":
            self._handle_reveal(data)
        elif self.path == "/api/settings/output-dir":
            self._handle_set_output_dir(data)
        elif self.path == "/api/settings/library-dir":
            self._handle_set_library_dir(data)
        elif self.path == "/api/settings":
            self._handle_set_prefs(data)
        elif self.path == "/api/library/transfer":
            self._handle_transfer(data)
        elif self.path == "/api/library/chapter-names":
            self._handle_chapter_names(data)
        elif match := re.match(r"/api/queue/([^/]+)/cancel$", self.path):
            ok = self.kernel["queue"].cancel(match.group(1))
            self._send_json({"success": ok})
        elif self.path == "/api/queue/clear":
            self._send_json({"removed": self.kernel["queue"].clear_finished()})
        elif self.path == "/api/watchlist":
            self._handle_watchlist_add(data)
        elif self.path == "/api/watchlist/clear-downloaded":
            self._send_json(
                {"removed": self.kernel["watchlist"].clear_downloaded()})
        elif match := re.match(r"/api/watchlist/([^/]+)/remove$", self.path):
            removed = self.kernel["watchlist"].remove(unquote(match.group(1)))
            self._send_json({"success": removed})
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_status(self):
        auth = self.kernel["auth"]
        status = auth.get_status()
        self._send_json(status)

    @staticmethod
    def _int_param(params: dict, name: str, default: int, low: int, high: int) -> int:
        """Read an int query param, clamped. Bad input falls back to default."""
        try:
            value = int(params.get(name, [default])[0])
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def _handle_search(
        self,
        query: str,
        limit: int = 25,
        page: int = 0,
        language: str = "",
        sort: str = "",
        content_type: str = "book",
    ):
        if not query:
            self._send_json({"results": [], "total": 0, "page": 0, "has_more": False})
            return

        # Los audiolibros usan otro endpoint y otro pipeline (audio, no EPUB).
        if content_type == "audiobook":
            payload = self.kernel["audiobook"].search(
                query, limit=limit, page=page, language=language or None
            )
            payload["content_type"] = "audiobook"
            self._mark_library(payload["results"])
            self._send_json(payload)
            return

        book = self.kernel["book"]

        # Valores desconocidos se descartan en vez de reenviarse: un `sort`
        # inválido hace que la API devuelva HTTP 400 y tumbaría la búsqueda.
        if language not in book.LANGUAGES:
            language = ""
        if sort not in book.SORT_OPTIONS:
            sort = ""

        payload = book.search(
            query, limit=limit, page=page,
            language=language or None, sort=sort or None,
        )
        payload["filters"] = {"language": language, "sort": sort or "relevance"}

        self._mark_library(payload["results"])
        self._send_json(payload)

    def _mark_library(self, results: list):
        """Marca cada resultado con lo que ya sabemos de el, para los cintillos.

        Dos cosas distintas: si ya esta descargado, y si esta guardado para
        despues. Un titulo puede estar en los dos estados a la vez.
        """
        # La verdad es lo que se ve en "Mi biblioteca", asi que se pregunta a
        # la misma fuente: library.scan(), que fusiona lo publicado con lo que
        # sigue en la cache local pero ya esta completo.
        #
        # Antes esto salia de output.list_downloaded(), que solo busca el
        # marcador .book_id dentro de output/. Ese marcador lo escribe
        # create_book_dir() ANTES de bajar el primer capitulo, asi que una
        # descarga cancelada o caida dejaba el cintillo "EN LA BIBLIOTECA"
        # puesto para siempre sobre un libro que no existe en ningun lado.
        library = {}
        for entry in self.kernel["library"].scan():
            book_id = str(entry.get("book_id") or "")
            if book_id:
                library[book_id] = entry.get("folder") or ""

        saved = self.kernel["watchlist"].ids()
        for item in results:
            book_id = str(item.get("id"))
            item["in_library"] = book_id in library
            item["in_watchlist"] = book_id in saved
            if item["in_library"]:
                item["library_folder"] = library[book_id]

    def _handle_audiobook_info(self, book_id: str):
        try:
            self._send_json(self.kernel["audiobook"].fetch(book_id))
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_audiobook_chapters(self, book_id: str):
        """Capitulos del audiolibro para el selector de la UI."""
        try:
            chapters = self.kernel["audiobook"].fetch_chapters(book_id)
            self._send_json({
                "chapters": [
                    {"index": c.index, "title": c.title,
                     "minutes": round(c.duration / 60, 1) if c.duration else None}
                    for c in chapters
                ],
                "total": len(chapters),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_book_info(self, book_id: str):
        book = self.kernel["book"]
        try:
            info = book.fetch(book_id)
            self._send_json(info)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_chapters_list(self, book_id: str):
        """Return list of chapters for chapter selection UI."""
        chapters_plugin = self.kernel["chapters"]
        try:
            chapters = chapters_plugin.fetch_list(book_id)
            result = {
                "chapters": [
                    {
                        "index": i,
                        "title": ch.get("title", f"Chapter {i + 1}"),
                        "pages": ch.get("virtual_pages"),
                        "minutes": ch.get("minutes_required"),
                    }
                    for i, ch in enumerate(chapters)
                ],
                "total": len(chapters),
            }
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_progress(self):
        """Alias del trabajo en curso, en la forma que espera la UI de siempre.

        Se mantiene mientras el frontend migra a /api/queue: asi meter la cola
        no rompe la pantalla de descarga de golpe.
        """
        job = self.kernel["queue"].running_job()
        if not job:
            self._send_json({})
            return

        payload = {
            "status": job["phase"] or job["status"],
            "book_id": job["book_id"],
            "percentage": job["percentage"],
            "message": job["message"],
            "current_chapter": job["current_chapter"],
            "total_chapters": job["total_chapters"],
            "chapter_title": "",
            "eta_seconds": None,
            "resumed_chapters": 0,
        }
        if job["status"] == "completed":
            payload.update({"status": "completed", "percentage": 100})
            payload.update(job["files"] or {})
            if job["format_errors"]:
                payload["format_errors"] = job["format_errors"]
        elif job["status"] == "error":
            payload.update({"status": "error", "error": job["error"]})
        elif job["status"] == "cancelled":
            payload["status"] = "cancelled"
        elif job["status"] == "paused":
            payload.update({"status": "paused", "error": job["message"]})

        self._send_json(payload)

    def _handle_get_settings(self):
        """Return current settings."""
        library = self.kernel["library"]
        configured = Path(config.LIBRARY_DIR)
        self._send_json(
            {
                # Cache de descarga: interna, no se elige por descarga
                "output_dir": str(config.OUTPUT_DIR),
                # Carpeta donde vive la biblioteca publicada
                "library_dir": str(configured),
                "library_default": str(config.DEFAULT_LIBRARY_DIR),
                "library_is_default": configured == Path(config.DEFAULT_LIBRARY_DIR),
                # Configurada pero inalcanzable (unidad de red caida, disco
                # desconectado): la UI avisa en vez de mostrarla vacia.
                "library_available": library.root() is not None,
                # Preferencia global. "omitir imagenes" NO esta aqui a
                # proposito: se decide en cada descarga, no una vez para todas.
                "transfer_after": config.SETTINGS.get("transfer_after", True),
            }
        )

    # Preferencias booleanas que acepta POST /api/settings
    BOOL_PREFS = ("transfer_after",)

    def _handle_set_prefs(self, data: dict):
        """Guarda las preferencias globales de descarga.

        Solo se tocan las claves presentes en la peticion: un cliente que manda
        una sola no pisa las demas con sus defaults.
        """
        saved = {}
        for key in self.BOOL_PREFS:
            if key in data:
                value = bool(data[key])
                config.save_setting(key, value)
                saved[key] = value

        if not saved:
            self._send_json({"error": "Nada que guardar"}, 400)
            return
        self._send_json({"success": True, "saved": saved})

    def _library_item(self, folder: str) -> dict | None:
        """Busca una obra por su `folder` en el indice ya construido.

        El nombre de la URL solo se usa para comparar, nunca para armar una
        ruta: la ruta sale del indice, asi que no hay forma de escapar de la
        biblioteca con un `..`.
        """
        return next(
            (i for i in self.kernel["library"].scan() if i.get("folder") == folder),
            None,
        )

    def _handle_library_tracks(self, folder: str):
        """Pistas de un audiolibro, en orden, para el reproductor."""
        item = self._library_item(folder)
        if item is None:
            self._send_json({"error": "not found"}, 404)
            return

        library = self.kernel["library"]
        tracks = library.tracks_for(item)
        self._send_json({
            "folder": folder,
            "title": item.get("title"),
            "authors": item.get("authors") or [],
            "year": item.get("year"),
            "cover_url": item.get("cover_url"),
            "content_type": item.get("content_type"),
            # Para que el reproductor pueda decir si la descarga quedo a medias:
            # duracion que deberia tener y cuantas pistas se esperaban.
            "duration_seconds": item.get("duration_seconds"),
            "expected_tracks": item.get("expected_tracks"),
            "audio_seconds": item.get("audio_seconds"),
            "incomplete": bool(item.get("incomplete")),
            # `titled` en falso significa que la obra se descargo antes de que
            # se guardaran los nombres de capitulo: la UI lo dice en vez de
            # inventarlos.
            "tracks": [dict(t, url=f"/api/library/audio/{folder}/{t['n']}")
                       for t in tracks],
        })

    def _handle_library_audio(self, folder: str, n: int):
        """Sirve una pista con soporte de Range."""
        item = self._library_item(folder)
        if item is None:
            self._send_json({"error": "not found"}, 404)
            return
        track = self.kernel["library"].track_path(item, n)
        if track is None or not track.is_file():
            self._send_json({"error": "track not found"}, 404)
            return

        mime = "audio/mpeg" if track.suffix.lower() == ".mp3" else "audio/mp4"
        self._serve_media(track, mime)

    def _handle_library_file(self, folder: str, kind: str):
        """Entrega el epub/pdf de una obra, para el lector embebido.

        Va por _serve_media, asi que responde a Range: epub.js se trae el
        archivo entero, pero un pdf grande se puede ir leyendo por partes.
        """
        item = self._library_item(folder)
        if item is None:
            self._send_json({"error": "not found"}, 404)
            return
        target = self.kernel["library"].book_path(item, kind)
        if target is None or not target.is_file():
            self._send_json({"error": f"no hay {kind} para esta obra"}, 404)
            return

        mime = ("application/pdf" if kind == "pdf"
                else "application/epub+zip")
        self._serve_media(target, mime)

    def _serve_media(self, path: Path, content_type: str):
        """Envia un archivo, respondiendo a `Range` con 206.

        `<audio>` pide rangos para buscar dentro de la pista. Sin 206 el
        navegador tiene que descargar el archivo completo cada vez que mueves la
        barra, y en varios navegadores la busqueda simplemente no funciona.
        """
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False

        raw = self.headers.get("Range")
        if raw:
            match = re.match(r"bytes=(\d*)-(\d*)\s*$", raw.strip())
            if match:
                first, last = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                elif last:
                    start = max(0, size - int(last))  # los ultimos N bytes
                partial = start < size and start <= end

            if not partial:
                # Un rango imposible se contesta con 416, no sirviendo el
                # archivo entero como si nada hubiera pasado.
                self._media_response = True
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self._media_response = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    # Normal: el navegador corta la peticion en cada busqueda o
                    # cambio de pista. No es un error que valga reportar.
                    # En Windows el corte llega como ConnectionAbortedError
                    # (WinError 10053), no como BrokenPipeError.
                    return
                remaining -= len(chunk)

    def _handle_library_cover(self, folder_name: str):
        """Sirve la portada extraida del epub, desde disco.

        `folder_name` viene de la URL, asi que se resuelve y se comprueba que
        el resultado siga siendo hijo directo del directorio de salida: sin eso
        un `..%2f..` podria leer cualquier archivo del sistema.
        """
        # La biblioteca es una fusion de dos origenes y el nombre de la URL
        # pertenece a uno o al otro: en lo publicado es covers/<work_id>.jpg, y
        # en la cache local es <slug>/cover.jpg. Se prueban los dos, porque
        # desde aqui no hay forma de saber de que lado viene el nombre.
        library = self.kernel["library"]
        candidates: list[tuple[Path, Path]] = []
        published = library.root()
        if published is not None:
            covers = Path(published).resolve() / "covers"
            candidates.append((covers, covers / f"{folder_name}.jpg"))
        local = self.kernel["output"].get_default_dir().resolve()
        candidates.append((local, local / folder_name / "cover.jpg"))

        target = None
        for base, candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, ValueError):
                continue
            # `folder_name` viene de la URL: se comprueba que el resultado siga
            # dentro de la raiz para que un `..%2f..` no lea otro archivo.
            try:
                resolved.relative_to(base)
            except ValueError:
                continue
            if resolved.is_file():
                target = resolved
                break

        if target is None:
            self._send_json({"error": "not found"}, 404)
            return

        data = target.read_bytes()
        # El archivo se llama .jpg siempre, pero la portada del epub puede ser
        # PNG: el tipo se declara por los bytes, no por la extension.
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        self._media_response = True  # cacheable: la portada no cambia
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # La portada nunca cambia para un libro dado
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # Claves de facet que acepta el visor de biblioteca
    LIBRARY_FACETS = ("location", "content_type", "language", "year",
                      "publishers", "authors", "formats")

    def _handle_library(self, params: dict):
        """Biblioteca local filtrada + conteos para el sidebar.

        Los facets llegan como listas separadas por coma, p. ej.
        ?language=en,es&publishers=Manning%20Publications
        El valor especial `__none__` selecciona los items sin ese dato.
        """
        query = (params.get("q", [""])[0] or "").strip()
        refresh = (params.get("refresh", ["0"])[0] or "0") not in ("0", "", "false")
        sort = (params.get("sort", ["title"])[0] or "title").strip()

        facets = {}
        for key in self.LIBRARY_FACETS:
            raw = params.get(key, [""])[0] or ""
            values = [v for v in (x.strip() for x in raw.split(",")) if v]
            if values:
                facets[key] = values

        try:
            payload = self.kernel["library"].browse(
                query=query, facets=facets, refresh=refresh, sort=sort
            )
            payload["applied"] = facets
            payload["library_dir"] = str(config.LIBRARY_DIR)
            # Si esta configurada pero no responde, el visor lo dice en vez de
            # dar a entender que no tienes nada publicado.
            payload["library_available"] = self.kernel["library"].root() is not None
            self._tag_bundles(payload.get("items") or [])
            self._send_json(payload)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _tag_bundles(self, items: list) -> None:
        """Mark the library works that came from a bundle.

        Read from the manifests on disk instead of stamping the fact onto each
        work: the bundle already describes itself in output/bundles, and
        copying that into every work's metadata would give it two places to
        disagree with itself.
        """
        try:
            manifests = self.kernel["bundle"].list_all()
        except Exception:  # noqa: BLE001
            return
        if not manifests:
            return

        index = {}
        for manifest in manifests:
            for language, entry in (manifest.get("languages") or {}).items():
                book_id = str(entry.get("book_id") or "")
                if book_id:
                    index[book_id] = {
                        "bundle_id": manifest.get("bundle_id"),
                        "bundle_lang": language,
                        "bundle_title": manifest.get("title"),
                        "bundle_complete": bool(manifest.get("complete")),
                    }

        for item in items:
            info = index.get(str(item.get("book_id") or ""))
            if info:
                item.update(info)

    def _handle_watchlist_add(self, data: dict):
        """Guarda un titulo para descargarlo mas tarde.

        Se guardan los datos que ya trae la busqueda (titulo, autores, portada)
        para poder pintar la lista sin sesion valida: si solo guardaramos el id,
        abrir "Para despues" con las cookies caducadas mostraria una lista de
        numeros.
        """
        try:
            entry = self.kernel["watchlist"].add(data)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json({"success": True, "entry": entry})

    def _handle_chapter_names(self, data: dict):
        """Recupera los nombres de capitulo de un audiolibro ya descargado.

        Los nombres solo existen en la API de O'Reilly: los archivos se
        renombran a 001.m4a al publicar y no llevan etiquetas. Necesita sesion
        valida, asi que puede fallar por cookies caducadas — y eso se dice.
        """
        folder = (data.get("folder") or "").strip()
        item = self._library_item(folder)
        if item is None:
            self._send_json({"error": "no encuentro esa obra"}, 404)
            return
        if item.get("content_type") != "audiobook":
            self._send_json({"error": "solo aplica a audiolibros"}, 400)
            return
        book_id = item.get("book_id")
        if not book_id:
            self._send_json({"error": "la obra no tiene book_id"}, 400)
            return

        try:
            chapters = self.kernel["audiobook"].fetch_chapters(book_id)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({
                "error": f"no se pudieron pedir los capítulos: {exc}. "
                         "Si la sesión caducó, pega cookies nuevas."
            }, 502)
            return

        library = self.kernel["library"]
        written = library.set_track_titles(item, chapters)
        if not written:
            self._send_json({"error": "la API no devolvió nombres usables"}, 502)
            return

        library.rebuild_index()
        self._send_json({"success": True, "titled": written,
                         "chapters": len(chapters)})

    def _handle_transfer(self, data: dict):
        """Encola el paso a la biblioteca de una o varias obras en cache.

        Acepta {"folder": "x"}, {"folders": [...]} o {"all": true}.
        """
        library = self.kernel["library"]
        if library.ensure_root() is None:
            self._send_json(
                {"error": f"La carpeta de la biblioteca no está accesible: "
                          f"{config.LIBRARY_DIR}"}, 409)
            return

        with self._transfer_lock:
            if self.transfer_progress.get("status") == "transferring":
                self._send_json({"error": "Ya hay una transferencia en curso"}, 409)
                return

        if data.get("all"):
            folders = [i["folder"] for i in library.scan()
                       if i.get("location") == "local"]
        else:
            folders = data.get("folders") or (
                [data["folder"]] if data.get("folder") else [])

        if not folders:
            self._send_json({"error": "No hay nada en local que transferir"}, 400)
            return

        # El estado se publica antes de arrancar el hilo: si el cliente pregunta
        # de inmediato, encuentra la cola ya empezada y no un hueco.
        self._set_transfer({"status": "transferring", "total": len(folders),
                            "index": 0, "current": folders[0], "done": [],
                            "failed": {}, "percentage": 0})
        threading.Thread(
            target=self._run_transfers, args=(folders,), daemon=True
        ).start()
        self._send_json({"status": "started", "count": len(folders)})

    def _run_transfers(self, folders: list[str]):
        """Transfiere la cola, una obra a la vez.

        Secuencial a proposito: si la carpeta esta en un recurso remoto, las
        copias en paralelo no van mas rapido (el coste es latencia por archivo,
        no ancho de banda), el progreso es honesto y un fallo se queda en una
        sola obra. El indice se reconstruye una vez al final, no por obra: es un
        escaneo completo del almacen.
        """
        library = self.kernel["library"]
        done: list[str] = []
        failed: dict[str, str] = {}

        for n, folder in enumerate(folders, start=1):
            self._update_transfer(current=folder, index=n, percentage=0)
            try:
                entry = next(
                    (i for i in library.scan()
                     if i.get("folder") == folder and i.get("location") == "local"),
                    None,
                )
                if entry is None:
                    raise FileNotFoundError(f"'{folder}' ya no está en la caché")
                if not entry.get("book_id"):
                    raise RuntimeError(
                        f"'{folder}' no tiene .book_id: sin él no se puede "
                        "calcular su dirección en la biblioteca")

                library.transfer_object(
                    Path(entry["path"]),
                    entry["book_id"],
                    entry.get("content_type") or "book",
                    on_progress=lambda copied, total, **_: self._update_transfer(
                        percentage=int(copied * 100 / total)),
                )
                done.append(folder)
            except Exception as exc:
                traceback.print_exc()
                failed[folder] = f"{type(exc).__name__}: {exc}"
            self._update_transfer(done=list(done), failed=dict(failed))

        objects = library.rebuild_index() if done else 0
        self._set_transfer({"status": "completed", "total": len(folders),
                            "done": done, "failed": failed,
                            "objects": objects, "percentage": 100})

    def _handle_search_filters(self):
        """Expose the filter vocabulary so the UI doesn't hardcode it."""
        book = self.kernel["book"]
        self._send_json({
            "languages": book.LANGUAGES,
            "sort_options": list(book.SORT_OPTIONS),
        })

    def _handle_formats(self):
        """Return available output formats for discovery.

        This endpoint allows any client (web, CLI, etc.) to discover
        supported formats, aliases, and which formats support chapter selection.
        """
        from plugins.downloader import DownloaderPlugin
        self._send_json(DownloaderPlugin.get_formats_info())

    def _handle_set_output_dir(self, data: dict):
        """Handle output directory selection - browse or direct path."""
        system_plugin = self.kernel["system"]
        output_plugin = self.kernel["output"]

        if data.get("browse"):
            # Open native folder picker dialog
            initial_dir = config.OUTPUT_DIR
            selected = system_plugin.show_folder_picker(initial_dir)
            if selected:
                self._send_json({"success": True, "path": str(selected)})
            else:
                self._send_json({"cancelled": True})
            return

        path_str = data.get("path", "").strip()

        if not path_str:
            self._send_json({"error": "path required"}, 400)
            return

        success, message, path = output_plugin.validate_dir(path_str)
        if not success:
            self._send_json({"error": message}, 400)
            return

        self._send_json({"success": True, "path": str(path)})

    def _handle_set_library_dir(self, data: dict):
        """Elige la carpeta donde vive la biblioteca.

        Es un ajuste global y persistente, no una opcion por descarga: la
        biblioteca es una sola. Ruta vacia = volver al default dentro de output,
        que es el caso de quien nunca elige nada.
        """
        library = self.kernel["library"]

        if data.get("browse"):
            start = library.root() or config.OUTPUT_DIR
            selected = self.kernel["system"].show_folder_picker(start)
            if selected:
                self._send_json({"success": True, "path": str(selected)})
            else:
                self._send_json({"cancelled": True})
            return

        raw = (data.get("path") or "").strip()
        target = Path(raw) if raw else Path(config.DEFAULT_LIBRARY_DIR)

        ok, message, path = self.kernel["output"].validate_dir(target)
        if not ok:
            self._send_json({"error": message}, 400)
            return

        # Se prueba de verdad antes de guardar: se apunta config a la ruta
        # nueva y se intenta crear la estructura. Si falla, se deja el ajuste
        # anterior intacto en vez de dejar la app apuntando a la nada.
        previous = config.LIBRARY_DIR
        config.LIBRARY_DIR = path
        if library.ensure_root() is None:
            config.LIBRARY_DIR = previous
            self._send_json(
                {"error": f"No se pudo preparar la estructura en {path}"}, 400)
            return

        config.save_setting("library_dir", str(path))
        # Se indexa la raiz nueva: adopta lo que ya hubiera dentro (una
        # biblioteca anterior) o queda un indice vacio si es recien creada.
        # No se mueve nada de la raiz vieja: eso tiene que pedirse a proposito.
        objects = library.rebuild_index()
        self._send_json({
            "success": True,
            "path": str(path),
            "objects": objects,
            "is_default": path == Path(config.DEFAULT_LIBRARY_DIR),
        })

    def _handle_cookies(self, data: dict):
        """Save cookies from user input."""
        if not isinstance(data, dict) or not data:
            self._send_json({"error": "Invalid cookie data"}, 400)
            return

        try:
            config.COOKIES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.kernel.http.reload_cookies()
            # Cookies nuevas: lo que estuviera pausado por sesion caducada
            # puede seguir, y sigue desde donde iba.
            resumed = self.kernel["queue"].session_refreshed()
            self._send_json({"success": True, "resumed": resumed})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_cancel(self):
        """Cancela el trabajo en curso (compatibilidad con la UI actual)."""
        queue = self.kernel["queue"]
        running = queue.snapshot().get("running_id")
        if running and queue.cancel(running):
            self._send_json({"success": True, "message": "Cancel requested"})
        else:
            self._send_json({"success": False, "message": "No active download"})

    def _handle_reveal(self, data: dict):
        """Open file manager and select the specified file."""
        path_str = data.get("path", "")
        if not path_str:
            self._send_json({"error": "path required"}, 400)
            return

        path = Path(path_str).resolve()

        if not path.exists():
            self._send_json({"error": "Path does not exist"}, 404)
            return

        system_plugin = self.kernel["system"]
        success = system_plugin.reveal_in_file_manager(path)

        if success:
            self._send_json({"success": True})
        else:
            self._send_json({"error": "Failed to reveal file"}, 500)

    def _handle_editions(self, book_id: str, language: str = "es"):
        """Is there an edition of this book in `language`?

        Answers with a score and the matched title, never a bare yes: pairing
        editions is a guess (O'Reilly gives each its own ISBN and no link
        between them), and only the person reading both titles can tell whether
        the guess is right.
        """
        try:
            result = self.kernel["editions"].counterpart(book_id, language)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return

        # Que falta ya en disco, para no volver a generar lo que esta bien.
        if result.get("found") and result.get("candidate"):
            try:
                bundle = self.kernel["bundle"]
                plan = bundle.plan(result.get("source") or {}, result["candidate"])
                gap = bundle.gap(plan["bundle_id"],
                                 [spec["lang"] for spec in plan["jobs"]])
                gap["bundle_id"] = plan["bundle_id"]
                result["bundle"] = gap
            except Exception:  # noqa: BLE001
                pass  # sin esto el checkbox sigue sirviendo, solo sin detalle

        self._send_json(result)

    def _handle_bundle_download(self, book_id: str, data: dict, transfer: bool):
        """Queue both editions of a book as one bundle."""
        from plugins.bundle import BUNDLE_FORMATS

        language = (data.get("bundle_language") or "es").strip().lower()
        match = self.kernel["editions"].counterpart(book_id, language)

        if not match.get("found") or not match.get("candidate"):
            self._send_json(
                {"error": f"No hay edicion en '{language}' para este libro",
                 "reason": match.get("reason", "")},
                409,
            )
            return

        source = match.get("source") or {
            "id": book_id, "title": data.get("title") or book_id, "language": "en",
        }
        candidate = match["candidate"]

        bundle = self.kernel["bundle"]
        plan = bundle.plan(source, candidate)
        bundle.start(plan, source, candidate)

        # Solo lo que falta. Aviso honesto: esto NO acorta la descarga de
        # capitulos -- el pipeline los necesita para generar cualquier formato.
        # Lo que evita es regenerar y pisar los cuatro que ya estan bien.
        gap = bundle.gap(plan["bundle_id"], [s["lang"] for s in plan["jobs"]])

        jobs = []
        skipped = []
        for spec in plan["jobs"]:
            if not spec["book_id"]:
                continue
            needed = gap["missing"].get(spec["lang"]) or []
            if not needed:
                skipped.append({"language": spec["lang"], "title": spec["title"]})
                continue
            job = self.kernel["queue"].enqueue(
                book_id=spec["book_id"],
                title=spec["title"],
                content_type="book",
                formats=list(needed),
                # A bundle promises every format with images, and the whole
                # book: a chapter selection would not even mean the same thing
                # in the other edition.
                skip_images=False,
                chapters=None,
                transfer=transfer,
                bundle_id=plan["bundle_id"],
                bundle_lang=spec["lang"],
            )
            jobs.append({
                "job_id": job.id, "book_id": spec["book_id"],
                "language": spec["lang"], "title": spec["title"],
                "formats": list(needed), "job_status": job.status,
            })

        self._send_json({
            "status": "queued" if jobs else "complete", "bundle": True,
            "bundle_id": plan["bundle_id"], "dir": plan["dir"],
            "score": match.get("score"), "jobs": jobs,
            "skipped": skipped, "gap": gap,
        })

    def _handle_download(self, data: dict):
        """Start a book download."""
        book_id = data.get("book_id")
        output_format = data.get("format", "epub")
        print(f"[DEBUG] Received format from request: '{output_format}' (raw data: {data.get('format')})")
        selected_chapters = data.get("chapters")
        output_dir_str = data.get("output_dir")
        chunking_opts = data.get("chunking", {})
        skip_images = bool(data.get("skip_images", False))
        target_lang = data.get("target_lang") or None
        content_type = data.get("content_type", "book")
        # Por defecto se pasa a la biblioteca al terminar; desactivarlo deja la
        # obra en la cache, con su boton para pasarla luego.
        transfer = bool(data.get("transfer",
                                 config.SETTINGS.get("transfer_after", True)))
        print(f"[DEBUG] target_lang={target_lang!r} content_type={content_type!r}")

        if not book_id:
            self._send_json({"error": "book_id required"}, 400)
            return

        # Validate target language against supported set (None = no translation)
        if target_lang and target_lang not in ("original", "en"):
            if target_lang not in config.TRANSLATE_LANGUAGES:
                self._send_json(
                    {"error": f"Unsupported target_lang '{target_lang}'"}, 400
                )
                return

        # Los audiolibros no comparten nada del pipeline de EPUB: se
        # despachan a su propio hilo antes de parsear formatos/chunking.
        if content_type == "audiobook":
            output_plugin = self.kernel["output"]
            if output_dir_str:
                ok, message, output_dir = output_plugin.validate_dir(output_dir_str)
                if not ok:
                    self._send_json({"error": message}, 400)
                    return
            else:
                output_dir = output_plugin.get_default_dir()

            job = self.kernel["queue"].enqueue(
                book_id=book_id,
                title=data.get("title") or book_id,
                content_type="audiobook",
                chapters=selected_chapters,
                transfer=transfer,
            )
            self._send_json({"status": "queued", "book_id": book_id,
                             "job_id": job.id, "job_status": job.status})
            return

        # Parse chunking config
        chunk_config = None
        if chunking_opts:
            chunk_size = chunking_opts.get("chunk_size", 4000)
            overlap = chunking_opts.get("overlap", 200)
            chunk_config = ChunkConfig(
                chunk_size=chunk_size,
                overlap=overlap,
                respect_boundaries=True,
            )

        # Validate output directory
        output_plugin = self.kernel["output"]
        if output_dir_str:
            success, message, output_dir = output_plugin.validate_dir(output_dir_str)
            if not success:
                self._send_json({"error": message}, 400)
                return
        else:
            output_dir = output_plugin.get_default_dir()

        # Parse formats using plugin (single source of truth)
        from plugins.downloader import DownloaderPlugin
        formats = DownloaderPlugin.parse_formats(output_format)

        # Un bundle no es un formato mas: son dos descargas emparejadas, asi
        # que se despacha antes de encolar la normal.
        if data.get("bundle"):
            self._handle_bundle_download(book_id, data, transfer)
            return

        # A la cola. Ya no se rechaza la segunda descarga: se pone detras.
        job = self.kernel["queue"].enqueue(
            book_id=book_id,
            title=data.get("title") or book_id,
            content_type="book",
            formats=formats,
            target_lang=target_lang,
            skip_images=skip_images,
            transfer=transfer,
            chapters=selected_chapters,
            chunking=chunking_opts or None,
        )
        self._send_json({"status": "queued", "book_id": book_id,
                         "job_id": job.id, "job_status": job.status})

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def create_server(host: str = "localhost", port: int = 8000) -> ThreadingHTTPServer:
    """Create and configure the HTTP server.

    Uses a threading server so a single slow or stalled client (or a long
    download) cannot block status/progress/cookie requests.
    """
    kernel = create_default_kernel()
    DownloaderHandler.kernel = kernel

    # Una cola pausada esperando cookies puede durar horas; reiniciar el
    # servidor no deberia costarte lo que ya habias encolado.
    queue = kernel["queue"]
    # Solo un servidor EJECUTA la cola: dos apuntando al mismo data/ bajarian lo
    # mismo a la vez, sobre las mismas carpetas y al doble de peticiones. Pero
    # leerla la leen los dos, para que el segundo pueda mostrarla — el worker es
    # lo unico que queda reservado al dueno.
    owner = queue.claim()
    restored = queue.load()
    if restored:
        print(f"[QUEUE] {restored} descarga(s) recuperadas de la sesion anterior")
    if not owner:
        print("[QUEUE] otro servidor ya esta ejecutando la cola: "
              "este solo la muestra, no descarga")

    server = ThreadingHTTPServer((host, port), DownloaderHandler)
    return server


def run_server(host: str = "localhost", port: int = 8000):
    """Start the HTTP server."""
    server = create_server(host, port)
    print(f"Server running at http://{host}:{port}")
    server.serve_forever()

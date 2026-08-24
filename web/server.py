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
from plugins.downloader import DownloadProgress
import config


class DownloaderHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the downloader web interface."""

    kernel: Kernel = None
    download_progress: dict = {}
    _progress_lock = threading.Lock()
    _cancel_requested: bool = False
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

    @classmethod
    def _set_progress(cls, data: dict):
        """Thread-safe progress replacement."""
        with cls._progress_lock:
            cls.download_progress = data

    @classmethod
    def _update_progress(cls, **kwargs):
        """Thread-safe progress update."""
        with cls._progress_lock:
            cls.download_progress.update(kwargs)

    def __init__(self, *args, **kwargs):
        self.static_dir = Path(__file__).parent / "static"
        super().__init__(*args, directory=str(self.static_dir), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

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
        elif match := re.match(r"/api/book/([^/]+)$", path):
            self._handle_book_info(match.group(1))
        elif path == "/api/progress":
            self._handle_progress()
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
        elif match := re.match(r"/api/library/cover/(.+)$", path):
            self._handle_library_cover(unquote(match.group(1)))
        else:
            super().do_GET()

    def send_response(self, code, message=None):
        """Tell browsers never to cache the static assets.

        Without this, an edited app.js/style.css keeps being served from the
        browser cache and the UI silently runs stale code after a change.
        """
        super().send_response(code, message)
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
        """Flag results already in the output dir, para el cintillo de la UI."""
        library = self.kernel["output"].list_downloaded()
        for item in results:
            folder = library.get(str(item.get("id")))
            item["in_library"] = folder is not None
            if folder:
                item["library_folder"] = folder

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
        with self._progress_lock:
            self._send_json(dict(self.download_progress))

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
            self._send_json(payload)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

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
            self._send_json({"success": True})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_cancel(self):
        """Request cancellation of the current download."""
        with self._progress_lock:
            status = self.download_progress.get("status")
            if status and status not in ("completed", "error", "cancelled"):
                DownloaderHandler._cancel_requested = True
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

            with self._progress_lock:
                status = self.download_progress.get("status")
                if status and status not in ("completed", "error", "cancelled"):
                    self._send_json({"error": "Download already in progress"}, 409)
                    return

            threading.Thread(
                target=self._download_audiobook_async,
                args=(book_id, output_dir, selected_chapters, transfer),
                daemon=True,
            ).start()
            self._send_json({"status": "started", "book_id": book_id})
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

        # Check if already downloading
        with self._progress_lock:
            status = self.download_progress.get("status")
            if status and status not in ("completed", "error", "cancelled"):
                self._send_json({"error": "Download already in progress"}, 409)
                return

        # Parse formats using plugin (single source of truth)
        from plugins.downloader import DownloaderPlugin
        formats = DownloaderPlugin.parse_formats(output_format)
        print(f"[DEBUG] Parsed formats: {formats}")

        # Start download in background thread
        thread = threading.Thread(
            target=self._download_book_async,
            args=(book_id, output_dir, formats, selected_chapters, skip_images,
                  chunk_config, target_lang, transfer),
            daemon=True,
        )
        thread.start()

        # Return immediately
        self._send_json({"status": "started", "book_id": book_id})

    def _download_book_async(
        self,
        book_id: str,
        output_dir: Path,
        formats: list[str],
        selected_chapters: list | None,
        skip_images: bool,
        chunk_config: ChunkConfig | None,
        target_lang: str | None = None,
        transfer: bool = True,
    ):
        """Background download wrapper with error handling."""
        # Reset cancel flag
        DownloaderHandler._cancel_requested = False

        try:
            downloader = self.kernel["downloader"]
            result = downloader.download(
                book_id=book_id,
                output_dir=output_dir,
                formats=formats,
                selected_chapters=selected_chapters,
                skip_images=skip_images,
                chunk_config=chunk_config,
                target_lang=target_lang,
                transfer=transfer,
                progress_callback=self._on_progress,
                cancel_check=lambda: DownloaderHandler._cancel_requested,
            )

            payload = {
                "status": "completed",
                "book_id": result.book_id,
                "title": result.title,
                "percentage": 100,
                **result.files,
            }
            # Formatos que fallaron: la descarga sirvió, pero el usuario debe
            # saber que falta algo en vez de creer que todo salió bien.
            if result.errors:
                payload["format_errors"] = result.errors
            self._set_progress(payload)
        except Exception as e:
            traceback.print_exc()
            error_msg = str(e)
            if "cancelled" in error_msg.lower():
                self._set_progress({"status": "cancelled", "error": error_msg})
            else:
                self._set_progress({"status": "error", "error": error_msg})

    def _download_audiobook_async(
        self,
        book_id: str,
        output_dir: Path,
        selected_chapters: list | None,
        transfer: bool = True,
    ):
        """Background audiobook download.

        El plugin reporta con kwargs sueltos (no DownloadProgress), asi que
        aqui se adaptan al mismo diccionario de progreso que consume la UI.
        """
        DownloaderHandler._cancel_requested = False

        def on_progress(**kw):
            payload = {"book_id": book_id, "content_type": "audiobook"}
            payload.update(kw)
            self._set_progress(payload)

        try:
            result = self.kernel["audiobook"].download(
                book_id=book_id,
                output_dir=output_dir,
                selected_chapters=selected_chapters,
                transfer=transfer,
                progress_callback=on_progress,
                cancel_check=lambda: DownloaderHandler._cancel_requested,
            )
            payload = {
                "status": "completed",
                "book_id": result.book_id,
                "title": result.title,
                "percentage": 100,
                "content_type": "audiobook",
                "audiobook": str(result.output_dir),
                "tracks": len(result.files),
            }
            if result.errors:
                payload["format_errors"] = result.errors
            self._set_progress(payload)
        except Exception as e:
            traceback.print_exc()
            cancelled = "cancel" in str(e).lower()
            self._set_progress({
                "status": "cancelled" if cancelled else "error",
                "book_id": book_id,
                "error": str(e),
                "content_type": "audiobook",
            })

    def _on_progress(self, progress: DownloadProgress):
        """Handle progress updates from the downloader plugin."""
        self._set_progress(
            {
                "status": progress.status,
                "book_id": progress.book_id,
                "percentage": progress.percentage,
                "message": progress.message,
                "eta_seconds": progress.eta_seconds,
                "current_chapter": progress.current_chapter,
                "total_chapters": progress.total_chapters,
                "chapter_title": progress.chapter_title,
            }
        )

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

    server = ThreadingHTTPServer((host, port), DownloaderHandler)
    return server


def run_server(host: str = "localhost", port: int = 8000):
    """Start the HTTP server."""
    server = create_server(host, port)
    print(f"Server running at http://{host}:{port}")
    server.serve_forever()

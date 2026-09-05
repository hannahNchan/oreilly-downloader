"""Download orchestration plugin."""

import shutil
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config
from plugins.base import Plugin
from plugins.chunking import ChunkConfig
from plugins.errors import PreviewOnly


@dataclass
class DownloadProgress:
    """Progress state for download operations."""

    status: str
    percentage: int = 0
    message: str = ""
    eta_seconds: int | None = None
    current_chapter: int = 0
    total_chapters: int = 0
    chapter_title: str = ""
    book_id: str = ""
    # Capitulos que ya estaban en la cache de reanudacion y no se volvieron a
    # pedir. 0 en una descarga normal.
    resumed_chapters: int = 0


@dataclass
class DownloadResult:
    """Result of a completed download."""

    book_id: str
    title: str
    output_dir: Path
    files: dict = field(default_factory=dict)  # {"epub": Path, "markdown": Path, ...}
    chapters_count: int = 0
    # {"epub": "UnicodeEncodeError: ..."} para los formatos que fallaron
    errors: dict = field(default_factory=dict)


class DownloaderPlugin(Plugin):
    """Orchestrates the complete book download workflow."""

    # Format vocabulary - discoverable by any client
    SUPPORTED_FORMATS = frozenset([
        "epub",
        "markdown",
        "markdown-chapters",
        "pdf",
        "pdf-chapters",
        "plaintext",
        "plaintext-chapters",
        "json",
        "jsonl",
        "toon",
        "chunks",
    ])

    # Aliases for user convenience (e.g., CLI shorthand)
    FORMAT_ALIASES = {
        "md": "markdown",
        "txt": "plaintext",
    }

    # Formats that only support entire book (no chapter selection)
    BOOK_ONLY_FORMATS = frozenset(["epub", "chunks"])

    @classmethod
    def parse_formats(cls, format_input: str | list[str]) -> list[str]:
        """Parse format specification into canonical format names."""
        # Handle list input
        if isinstance(format_input, list):
            raw_formats = format_input
        else:
            # Handle "all" special case
            if format_input == "all":
                return ["epub", "markdown", "pdf", "plaintext", "json", "toon", "chunks"]

            # Split comma-separated and clean
            raw_formats = [f.strip().lower() for f in format_input.split(",") if f.strip()]

        formats = []
        seen = set()

        for fmt in raw_formats:
            # Apply alias
            canonical = cls.FORMAT_ALIASES.get(fmt, fmt)

            # Handle special cases
            if canonical == "jsonl" and "json" not in seen:
                formats.append("json")
                seen.add("json")
            if canonical == "jsonl":
                formats.append("jsonl")
                seen.add("jsonl")
                continue

            # Skip invalid or duplicate
            if canonical not in cls.SUPPORTED_FORMATS or canonical in seen:
                continue

            formats.append(canonical)
            seen.add(canonical)

        return formats if formats else ["epub"]

    @classmethod
    def get_format_help(cls) -> dict[str, str]:
        """Return format descriptions for CLI help or UI display."""
        return {
            "epub": "Standard EPUB format (default)",
            "markdown": "Markdown files (alias: md)",
            "markdown-chapters": "Separate Markdown file per chapter",
            "pdf": "Single PDF file",
            "pdf-chapters": "Separate PDF per chapter",
            "plaintext": "Plain text (alias: txt)",
            "plaintext-chapters": "Separate text file per chapter",
            "json": "Structured JSON export",
            "jsonl": "JSON Lines format (includes json)",
            "toon": "Token-Oriented Object Notation (token-efficient JSON for LLMs)",
            "chunks": "Chunked content for LLM processing",
        }

    @classmethod
    def supports_chapter_selection(cls, fmt: str) -> bool:
        """Check if a format supports chapter selection."""
        canonical = cls.FORMAT_ALIASES.get(fmt, fmt)
        return canonical not in cls.BOOK_ONLY_FORMATS

    @classmethod
    def get_formats_info(cls) -> dict:
        """Return complete format information for discovery endpoints."""
        return {
            "formats": sorted(cls.SUPPORTED_FORMATS),
            "aliases": cls.FORMAT_ALIASES,
            "book_only": sorted(cls.BOOK_ONLY_FORMATS),
            "descriptions": cls.get_format_help(),
        }

    def download(
        self,
        book_id: str,
        output_dir: Path,
        formats: list[str] | None = None,
        selected_chapters: list[int] | None = None,
        skip_images: bool = False,
        chunk_config: ChunkConfig | None = None,
        target_lang: str | None = None,
        transfer: bool = True,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        """Orchestrate full download pipeline for a book."""
        if formats is None:
            formats = ["epub"]

        # Helper to report progress
        def report(
            status: str,
            percentage: int = 0,
            message: str = "",
            eta_seconds: int | None = None,
            current_chapter: int = 0,
            total_chapters: int = 0,
            chapter_title: str = "",
            resumed_chapters: int = 0,
        ):
            if progress_callback:
                progress_callback(
                    DownloadProgress(
                        status=status,
                        percentage=percentage,
                        message=message,
                        eta_seconds=eta_seconds,
                        current_chapter=current_chapter,
                        total_chapters=total_chapters,
                        chapter_title=chapter_title,
                        book_id=book_id,
                        resumed_chapters=resumed_chapters,
                    )
                )

        # Helper to check cancellation
        def check_cancel():
            if cancel_check and cancel_check():
                return True
            return False

        # Get plugins
        book_plugin = self.kernel["book"]
        chapters_plugin = self.kernel["chapters"]
        assets_plugin = self.kernel["assets"]
        html_processor = self.kernel["html_processor"]
        output_plugin = self.kernel["output"]
        translator = self.kernel["translator"]

        # Resolve translation up front so an unreachable service fails loudly
        # here (before doing all the download work) rather than silently.
        translate_enabled = bool(target_lang) and target_lang not in ("original", "en")
        print(f"[TRANSLATE] target_lang={target_lang!r} enabled={translate_enabled}")
        if translate_enabled and not translator.is_available():
            raise RuntimeError(
                f"Translation to '{target_lang}' requested but the local translation "
                f"service at {config.TRANSLATOR_URL} is not answering with a loaded "
                f"model. Start it with services/translator/run.ps1, or pick 'original'."
            )

        # Phase 1: Fetch metadata.
        #
        # We intentionally do NOT hard-fail here on the local JWT expiry check.
        # O'Reilly serves content for a grace period past the token's `exp`, and
        # the local clock check can lag the real session (e.g. cookies read from
        # the browser's on-disk store trail the live in-memory token). Let the
        # actual request be the source of truth — it raises a descriptive error
        # (expired vs. Akamai bot-block) if the session really is rejected.
        report("starting", 0)
        report("fetching_metadata", 5)
        book_info = book_plugin.fetch(book_id)

        # Phase 2: Fetch chapters list
        report("fetching_chapters", 10)
        all_chapters = chapters_plugin.fetch_list(book_id)
        toc = chapters_plugin.fetch_toc(book_id)
        all_chapters = chapters_plugin.reorder_by_toc(all_chapters, toc)

        # Filter chapters if selection provided
        if selected_chapters is not None:
            selected_set = set(selected_chapters)
            chapters = [ch for i, ch in enumerate(all_chapters) if i in selected_set]
        else:
            chapters = all_chapters

        # Create output directory
        book_dir = output_plugin.create_book_dir(
            output_dir=output_dir,
            book_id=book_id,
            title=book_info.get("title", ""),
            authors=book_info.get("authors"),
            # Una traduccion del mismo libro es otra descarga del mismo book_id.
            # Sin sufijo las dos escriben en la misma carpeta: es el mismo motivo
            # por el que la cola deduplica.
            suffix=(f"-{config.language_tag(target_lang, fallback='')}"
                    if translate_enabled else ""),
        )
        oebps = output_plugin.get_oebps_dir(book_dir)

        # Cache de reanudacion. El token de O'Reilly dura ~15 minutos y un libro
        # grande son cientos de peticiones: si la sesion muere a mitad, sin esto
        # el reintento empieza de cero. Se guarda el HTML CRUDO, que es lo unico
        # que cuesta red; procesarlo y traducirlo se recalcula offline y gratis.
        cache_dir = book_dir / ".cache" / "chapters"
        cache_dir.mkdir(parents=True, exist_ok=True)

        def cache_path(name: str) -> Path:
            # El nombre del capitulo puede traer subcarpetas; se aplanan para
            # que la cache sea un solo directorio plano.
            return cache_dir / (name.replace("/", "__") + ".html")

        ya_en_cache = sum(
            1 for ch in chapters
            if cache_path(ch["filename"].replace(".html", ".xhtml")).is_file()
        )
        if ya_en_cache:
            print(f"[RESUME] {ya_en_cache}/{len(chapters)} capitulos ya en cache, "
                  "no se vuelven a pedir")

        # Phase 3: Download cover
        if not skip_images:
            report("downloading_cover", 12)
            cover_url = book_info.get("cover_url")
            if cover_url:
                images_dir = output_plugin.get_images_dir(book_dir)
                images_dir.mkdir(parents=True, exist_ok=True)
                assets_plugin.download_image(cover_url, images_dir / "cover.jpg")

        # Phase 4: Process chapters
        # url -> el numero que le toca en Styles/StyleNN.css.
        #
        # Un dict ordenado y no un set: el numero asignado a una hoja no puede
        # cambiar a mitad de descarga, y `list(css_index)` mas abajo tiene que
        # salir en ESE orden para que StyleNN.css sea de verdad la hoja NN.
        # Con un set, `list()` daba un orden arbitrario y "las primeras N" no
        # eran las hojas que el capitulo pedia.
        css_index: dict[str, int] = {}
        all_image_urls = set()
        chapters_data = []
        # Paginas que llegaron mas cortas de lo esperado y se aceptaron igual.
        # Se cuentan para decirlo al final en vez de callarlo.
        paginas_cortas: list[str] = []
        # El plugin es un singleton: sin esto las cuentas del libro anterior
        # decidirian sobre este.
        chapters_plugin.reset_stats()
        # (xhtml_filename, path_prefix, ch, css_refs) para la reescritura de la
        # traduccion.
        #
        # Las css_refs viajan aqui en vez de recalcularse: el capitulo traducido
        # tiene que enlazar EXACTAMENTE las mismas hojas que el original, y
        # recalcularlas era justo la causa del cambio de fuente.
        #
        # Y va el dict del capitulo entero, no una copia de su titulo: los
        # titulos se traducen despues de este bucle, y una copia se quedaria en
        # ingles. Por referencia, el titulo esta siempre al dia.
        chapter_files = []

        # Idioma que se declara en cada documento de contenido: el del libro
        # mientras no se traduzca, el de destino en la reescritura.
        #
        # Va en el propio XHTML y no solo en el OPF porque la especificacion de
        # EPUB dice que el lector NO debe deducir el idioma de un recurso a
        # partir del package document. Y el atributo no es decorativo: de el
        # cuelgan la seleccion de glifos del idioma, los selectores :lang() de
        # la hoja del editor, el silabeo y la voz del lector de pantalla.
        source_tag = config.language_tag(None, fallback=book_info.get("language") or "en")
        target_tag = config.language_tag(target_lang, fallback=source_tag)
        total_chapters = len(chapters)

        # ETA tracking
        chapter_times = []
        chapter_start_time = time.time()

        for i, ch in enumerate(chapters):
            if check_cancel():
                self._cleanup_on_cancel(book_dir)
                raise Exception("Download cancelled by user")

            # Calculate percentage (chapters are 15%-80% of work)
            chapter_pct = 15 + int((i / total_chapters) * 65) if total_chapters > 0 else 15

            report(
                "processing_chapters",
                chapter_pct,
                current_chapter=i + 1,
                total_chapters=total_chapters,
                chapter_title=ch.get("title", ""),
                resumed_chapters=ya_en_cache,
            )

            # Compute relative path prefix based on chapter depth in OEBPS
            filename = ch["filename"].replace(".html", ".xhtml")
            depth = filename.count("/")
            path_prefix = "../" * depth if depth > 0 else ""

            # Fetch and process chapter content. Con el capitulo ya en cache
            # se salta la red entera, incluido el REQUEST_DELAY de 0.5s por
            # peticion, que es lo que hace largo un reintento.
            cached = cache_path(filename)
            if cached.is_file() and cached.stat().st_size:
                raw_html = cached.read_text(encoding="utf-8")
                # Un capitulo grande que viene de cache tambien vale de patron
                # de control. Sin esto, reanudar una descarga dejaba a
                # fetch_content sin referencia con la que comprobar la sesion, y
                # cualquier pagina legitimamente corta pasaba por avance.
                chapters_plugin.seed_reference(ch["content_url"], len(raw_html))
            else:
                try:
                    raw_html = chapters_plugin.fetch_content(ch["content_url"])
                except PreviewOnly as exc:
                    # Ya hubo una ronda con cookies nuevas y sigue igual, asi que
                    # la sesion no es el problema. Se acepta lo que llego y se
                    # sigue: perder una pagina de relleno es infinitamente menos
                    # que perder el libro, que es lo que pasaba antes.
                    raw_html = getattr(exc, "html", "") or ""
                    paginas_cortas.append(filename)
                    print(f"[CHAPTERS] {filename} llego corto y se acepta tal "
                          f"cual. {exc}")
                # Escritura a .part + rename: un corte a mitad de escritura no
                # deja un capitulo truncado que luego se daria por completo.
                # fetch_content ya rechaza los stubs de preview, asi que aqui
                # solo llega HTML valido.
                tmp = cached.parent / (cached.name + ".part")
                tmp.write_text(raw_html, encoding="utf-8")
                tmp.replace(cached)
            processed, images = html_processor.process(
                raw_html, book_id, skip_images=skip_images, path_prefix=path_prefix
            )

            # Collect CSS and image URLs
            for css_url in ch["stylesheets"]:
                css_index.setdefault(css_url, len(css_index))
            for img_url in ch["images"]:
                all_image_urls.add(img_url)
            for img_url in images:
                if img_url.startswith("http") or img_url.startswith("/"):
                    all_image_urls.add(img_url)

            # Wrap in XHTML. Solo LAS SUYAS: antes se emitian "las primeras N
            # del set", con N creciendo capitulo a capitulo, asi que el capitulo
            # 1 enlazaba dos hojas y el 50 nueve, y ninguna era necesariamente
            # la que ese capitulo pedia. dict.fromkeys quita duplicados sin
            # perder el orden en que el capitulo las declara.
            css_refs = [
                f"{path_prefix}Styles/Style{css_index[url]:02d}.css"
                for url in dict.fromkeys(ch["stylesheets"])
            ]
            xhtml = html_processor.wrap_xhtml(
                processed, css_refs, ch["title"], lang=source_tag
            )

            # Write chapter file
            file_path = oebps / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(xhtml, encoding='utf-8')

            chapters_data.append((ch["filename"], ch["title"], processed))
            # Remember how to re-write this file if we translate later.
            chapter_files.append((filename, path_prefix, ch, css_refs))

            # Calculate ETA based on rolling average
            chapter_time = time.time() - chapter_start_time
            chapter_times.append(chapter_time)
            chapter_start_time = time.time()

            if chapter_times:
                avg_time = sum(chapter_times[-5:]) / len(chapter_times[-5:])
                remaining = total_chapters - (i + 1)
                eta_seconds = int(avg_time * remaining)
                report(
                    "processing_chapters",
                    chapter_pct,
                    eta_seconds=eta_seconds,
                    current_chapter=i + 1,
                    total_chapters=total_chapters,
                    chapter_title=ch.get("title", ""),
                )

        # Phase 5: Download assets
        report("downloading_assets", 80, eta_seconds=None)

        # Normalize image URLs
        image_list = []
        for img_url in all_image_urls:
            if img_url.startswith("/"):
                img_url = f"https://learning.oreilly.com{img_url}"
            image_list.append(img_url)

        # El orden importa: download_all_css escribe Style{i:02d}.css por el
        # indice en esta lista, y css_index ya asigno esos mismos numeros.
        css_list = list(css_index)
        total_assets = len(css_list) + len(image_list)

        # Download CSS
        css_width = len(str(len(css_list)))

        def css_progress(completed: int, total: int):
            if total_assets > 0:
                pct = 80 + int((completed / total_assets) * 10)
                report(
                    "downloading_assets",
                    pct,
                    f"{pct:2d}% - Downloading CSS ({completed:>{css_width}}/{len(css_list)})",
                )

        assets_plugin.download_all_css(css_list, oebps, progress_callback=css_progress)

        # Download assets referenced in CSS (e.g. url() images in ::after)
        if not skip_images:
            assets_plugin.download_css_assets(css_list, oebps)
            # Inline CSS content:url() images as <img> tags (Apple Books compat)
            html_processor.inline_css_content_images(oebps)

        # Download images
        if not skip_images:
            img_width = len(str(len(image_list)))

            def image_progress(completed: int, total: int):
                if total_assets > 0:
                    pct = 80 + int(((len(css_list) + completed) / total_assets) * 10)
                    report(
                        "downloading_assets",
                        pct,
                        f"{pct:2d}% - Downloading images ({completed:>{img_width}}/{len(image_list)})",
                    )

            assets_plugin.download_all_images(
                image_list, oebps, progress_callback=image_progress
            )

        # Phase 5.5: Translate.
        #
        # Deliberately done AFTER every O'Reilly request has completed.
        # Translation adds tens of seconds per chapter, and running it inside
        # the fetch loop stretched a download long enough for the session to
        # expire, at which point O'Reilly answers 200 with a short preview stub
        # and the book silently loses all but the first paragraph per chapter.
        # Fetching everything first keeps all network I/O inside one fresh
        # session; the slow part now runs with no session dependency at all.
        if translate_enabled:
            # Titulos e indice primero: texto plano, sin marcado, una sola tanda.
            # Es la parte mas visible del libro y la mas facil de traducir bien,
            # y dejarla en ingles dentro de un libro en espanol se ve al abrirlo.
            # El indice se muta en su sitio, asi que esto arregla de una vez el
            # nav del EPUB, el toc.ncx y el indice del PDF.
            report("translating_titles", 89, message="Traduciendo titulos e indice")
            chapter_titles = [ch.get("title") or "" for ch in chapters]
            for ch, new_title in zip(
                chapters, translator.translate_texts(chapter_titles, target_lang)
            ):
                ch["title"] = new_title
            translator.translate_toc(toc, target_lang)

            translated_data = []
            for i, (filename, title, processed) in enumerate(chapters_data):
                if check_cancel():
                    self._cleanup_on_cancel(book_dir)
                    raise Exception("Download cancelled by user")

                pct = 90 + int((i / len(chapters_data)) * 5) if chapters_data else 90

                # El titulo ya traducido, leido del dict del capitulo. No de
                # chapters_data, que se lleno ANTES de traducir: de ahi saldria
                # el ingles, y ese mismo titulo va al XHTML, al Markdown, al
                # JSON y al texto plano.
                xhtml_name, path_prefix, ch_ref, css_refs = chapter_files[i]
                ch_title = ch_ref.get("title") or title
                report(
                    "translating_chapters",
                    pct,
                    message=f"Traduciendo capítulo {i + 1}/{len(chapters_data)}",
                    current_chapter=i + 1,
                    total_chapters=len(chapters_data),
                    chapter_title=ch_title,
                )

                translated = translator.translate_html(processed, target_lang)
                translated_data.append((filename, ch_title, translated))

                # Re-write the on-disk XHTML so EPUB/PDF pick up the translation.
                xhtml = html_processor.wrap_xhtml(
                    translated, css_refs, ch_title, lang=target_tag
                )
                (oebps / xhtml_name).write_text(xhtml, encoding="utf-8")

            chapters_data = translated_data

        # Phase 6: Generate output formats
        result = DownloadResult(
            book_id=book_id,
            title=book_info.get("title", ""),
            output_dir=book_dir,
            chapters_count=len(chapters_data),
        )

        def guard(fmt: str, produce):
            """Run one format generator; one failure must not lose the rest.

            A download is 20+ minutes of fetching. Before this, a single bad
            character while writing one metadata file discarded every format,
            including the chapters already on disk. Now the failure is recorded
            and the remaining formats still get generated.
            """
            try:
                produce()
            except Exception as exc:
                result.errors[fmt] = f"{type(exc).__name__}: {exc}"
                print(f"[WARN] no se pudo generar '{fmt}': {exc}")
                traceback.print_exc()

        # Orden de generacion, y es deliberado: Markdown, JSON, texto plano,
        # PDF y EPUB al final. Los baratos primero, para que un fallo en el PDF
        # (WeasyPrint es lo lento y lo fragil de la lista) no te deje sin los
        # tres que ya se podian haber escrito. `formats` solo dice QUE generar;
        # el ORDEN lo decide esta secuencia, y los porcentajes de `report` van
        # con ella: si mueves un bloque, renumeralos o la barra retrocede.
        # Markdown
        if "markdown" in formats or "md" in formats or "markdown-chapters" in formats:
            def _make_markdown():
                report("generating_markdown", 90)
                md_plugin = self.kernel["markdown"]
                md_plugin.generate_book(book_info, chapters_data, book_dir)
                result.files["markdown"] = str(book_dir / "Markdown")
            guard("markdown", _make_markdown)
        # JSON
        if "json" in formats:
            def _make_json():
                report("generating_json", 91)
                json_plugin = self.kernel["json_export"]
                json_path = json_plugin.generate(
                    book_dir=book_dir,
                    book_metadata=book_info,
                    chapters_data=chapters_data,
                    include_jsonl="jsonl" in formats,
                )
                result.files["json"] = str(json_path)
            guard("json", _make_json)
        # Plain text
        if "plaintext" in formats or "txt" in formats or "plaintext-chapters" in formats:
            def _make_plaintext():
                report("generating_plaintext", 92)
                plaintext_plugin = self.kernel["plaintext"]
                single_file = "plaintext-chapters" not in formats
                txt_path = plaintext_plugin.generate(
                    book_dir=book_dir,
                    book_metadata=book_info,
                    chapters_data=chapters_data,
                    single_file=single_file,
                )
                result.files["plaintext"] = str(txt_path)
            guard("plaintext", _make_plaintext)
        # PDF
        if "pdf" in formats or "all" in formats or "pdf-chapters" in formats:
            def _make_pdf():
                pdf_plugin = self.kernel["pdf"]

                if "pdf-chapters" in formats:
                    report("generating_pdf_chapters", 94)
                    pdf_paths = pdf_plugin.generate_chapters(
                        book_info=book_info,
                        chapters=chapters,
                        output_dir=book_dir,
                        css_files=css_list,
                        language=target_tag,
                    )
                    result.files["pdf"] = [str(p) for p in pdf_paths]
                else:
                    report("generating_pdf", 94)
                    pdf_path = pdf_plugin.generate(
                        book_info=book_info,
                        chapters=chapters,
                        toc=toc,
                        output_dir=book_dir,
                        css_files=css_list,
                        cover_image="cover.jpg",
                        language=target_tag,
                    )
                    result.files["pdf"] = str(pdf_path)
            guard("pdf", _make_pdf)
        # EPUB
        if "epub" in formats:
            def _make_epub():
                report("generating_epub", 96)
                epub_plugin = self.kernel["epub"]
                epub_path = epub_plugin.generate(
                    book_info=book_info,
                    chapters=chapters,
                    toc=toc,
                    output_dir=book_dir,
                    css_files=css_list,
                    cover_image="cover.jpg",
                    language=target_tag,
                )
                result.files["epub"] = str(epub_path)
            guard("epub", _make_epub)
        # TOON
        if "toon" in formats:
            def _make_toon():
                report("generating_toon", 97)
                toon_plugin = self.kernel["toon_export"]
                toon_path = toon_plugin.generate(
                    book_dir=book_dir,
                    book_metadata=book_info,
                    chapters_data=chapters_data,
                )
                result.files["toon"] = str(toon_path)
            guard("toon", _make_toon)
        # Chunks
        if "chunks" in formats:
            def _make_chunks():
                report("generating_chunks", 98)
                chunking_plugin = self.kernel["chunking"]
                chunks_path = chunking_plugin.generate(
                    book_dir=book_dir,
                    book_metadata=book_info,
                    chapters_data=chapters_data,
                    config=chunk_config,
                )
                result.files["chunks"] = str(chunks_path)
            guard("chunks", _make_chunks)

        # La descarga acabo: la cache de reanudacion ya no sirve de nada y no
        # debe viajar a la biblioteca. Solo se borra en el camino de exito; si
        # algo falla antes, sobrevive y el siguiente intento la aprovecha.
        shutil.rmtree(book_dir / ".cache", ignore_errors=True)

        # Forma canonica en la cache local: la obra queda exactamente como va a
        # quedar en la biblioteca, asi que publicarla es solo copiar.
        library = self.kernel.get("library")
        if library is not None:
            guard("finalize", lambda: library.finalize_object(book_dir, {
                "book_id": book_id,
                "content_type": "book",
                "title": book_info.get("title"),
                "authors": book_info.get("authors"),
                "publishers": book_info.get("publishers"),
                "language": book_info.get("language"),
                "date": book_info.get("publication_date"),
                "isbn": book_info.get("isbn"),
            }, rebuild=False))

            # La transferencia va aparte y con guard: si la red falla, la
            # descarga NO se pierde. Queda en local con su boton de transferir.
            if transfer and library.ensure_root():
                def publish():
                    report("transferring", 98)
                    moved = library.transfer_object(book_dir, book_id, "book")
                    # Una descarga parcial conserva los formatos que no rehizo.
                    # Se anuncia: un traspaso que toca la obra ya publicada no
                    # deberia pasar en silencio.
                    if moved.get("kept"):
                        print(f"[LIBRARY] {len(moved['kept'])} archivo(s) de la "
                              f"version anterior se conservan: "
                              f"{', '.join(sorted(moved['kept'])[:6])}")
                    library.rebuild_index()
                    new_dir = Path(moved["path"])

                    # transfer_object MUEVE la carpeta, asi que cada ruta de
                    # result.files apunta a un sitio que ya no existe. Antes
                    # solo se corregia output_dir, y quien leyera files se
                    # encontraba rutas muertas sin un solo error que lo dijera:
                    # el boton Reveal de la UI abria la nada, y el archivado de
                    # un bundle copiaba cero archivos y los daba por perdidos.
                    def rehome(value):
                        if isinstance(value, list):
                            return [rehome(item) for item in value]
                        try:
                            return str(new_dir / Path(value).relative_to(book_dir))
                        except ValueError:
                            return value  # fuera de book_dir: no es nuestro

                    result.files = {k: rehome(v) for k, v in result.files.items()}
                    result.output_dir = new_dir
                    result.files["library_rel"] = moved["rel"]
                guard("transfer", publish)

        # Veredicto. Publicar un libro de avances como si estuviera completo es
        # peor que fallar: se ve bien en la biblioteca, ocupa su sitio, y no lo
        # descubres hasta que lo abres. Medido en el caso real: capitulos de 800
        # bytes y 23 KB de texto para el libro entero, marcado "completed".
        cortas = len(chapters_plugin.short_accepted)
        completas = chapters_plugin.full_pages
        if cortas and completas == 0:
            # Con CERO paginas sanas no se puede distinguir la causa desde aqui,
            # asi que se dicen las dos en vez de inventar una. Lo que si es
            # seguro es que no hay libro que publicar.
            raise RuntimeError(
                f"Ninguna de las {cortas} paginas de este libro llego completa: "
                f"todas eran avances. O la sesion dejo de servir contenido a "
                f"mitad de la descarga, o este libro no esta disponible completo "
                f"con esta cuenta. No se genera nada, porque un EPUB de avances "
                f"parece correcto y no lo es."
            )
        if cortas > completas:
            print(f"[CHAPTERS] AVISO: {cortas} pagina(s) llegaron cortas frente "
                  f"a {completas} completas. El libro puede estar incompleto.")

        if paginas_cortas:
            print(f"[CHAPTERS] {len(paginas_cortas)} pagina(s) llegaron cortas y "
                  f"se aceptaron: {', '.join(paginas_cortas[:5])}")

        report("completed", 100)
        return result

    # Restos del armado del epub: intermedios, nunca contenido final
    BUILD_ARTIFACTS = ("OEBPS", "META-INF", "mimetype")

    def _cleanup_on_cancel(self, book_dir: Path):
        """Clean up partially downloaded book on cancellation.

        Con la biblioteca canonica esta carpeta es el objeto del libro, y el
        work_id es el mismo en cada descarga: un rmtree ciego se llevaria una
        copia ya completa al cancelar una re-descarga. Se borran los restos del
        build y la carpeta solo desaparece si no queda contenido dentro.
        """
        if not book_dir.exists():
            return

        for name in self.BUILD_ARTIFACTS:
            artifact = book_dir / name
            if artifact.is_dir():
                shutil.rmtree(artifact, ignore_errors=True)
            elif artifact.is_file():
                artifact.unlink(missing_ok=True)

        # metadata.json y cover.jpg son metadata nuestra: un objeto que solo
        # tenga eso es un cascaron y si se puede borrar.
        husk = {"metadata.json", "cover.jpg", "library.json"}
        leftover = [
            p for p in book_dir.iterdir()
            if p.name not in husk and not p.name.startswith(".")
        ]
        if not leftover:
            shutil.rmtree(book_dir, ignore_errors=True)

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

        # Resolve translation up front so an unreachable Ollama fails loudly
        # here (before doing all the download work) rather than silently.
        translate_enabled = bool(target_lang) and target_lang not in ("original", "en")
        print(f"[TRANSLATE] target_lang={target_lang!r} enabled={translate_enabled}")
        if translate_enabled and not translator.is_available():
            raise RuntimeError(
                f"Translation to '{target_lang}' requested but the Ollama server at "
                f"{config.OLLAMA_URL} is not reachable. Start Ollama or pick 'original'."
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
        )
        oebps = output_plugin.get_oebps_dir(book_dir)

        # Phase 3: Download cover
        if not skip_images:
            report("downloading_cover", 12)
            cover_url = book_info.get("cover_url")
            if cover_url:
                images_dir = output_plugin.get_images_dir(book_dir)
                images_dir.mkdir(parents=True, exist_ok=True)
                assets_plugin.download_image(cover_url, images_dir / "cover.jpg")

        # Phase 4: Process chapters
        all_css_urls = set()
        all_image_urls = set()
        chapters_data = []
        chapter_files = []  # (xhtml_filename, path_prefix, title) for translation rewrite
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
            )

            # Compute relative path prefix based on chapter depth in OEBPS
            filename = ch["filename"].replace(".html", ".xhtml")
            depth = filename.count("/")
            path_prefix = "../" * depth if depth > 0 else ""

            # Fetch and process chapter content
            raw_html = chapters_plugin.fetch_content(ch["content_url"])
            processed, images = html_processor.process(
                raw_html, book_id, skip_images=skip_images, path_prefix=path_prefix
            )

            # Collect CSS and image URLs
            all_css_urls.update(ch["stylesheets"])
            for img_url in ch["images"]:
                all_image_urls.add(img_url)
            for img_url in images:
                if img_url.startswith("http") or img_url.startswith("/"):
                    all_image_urls.add(img_url)

            # Wrap in XHTML
            css_refs = [f"{path_prefix}Styles/Style{j:02d}.css" for j in range(len(all_css_urls))]
            xhtml = html_processor.wrap_xhtml(processed, css_refs, ch["title"])

            # Write chapter file
            file_path = oebps / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(xhtml, encoding='utf-8')

            chapters_data.append((ch["filename"], ch["title"], processed))
            # Remember how to re-write this file if we translate later.
            chapter_files.append((filename, path_prefix, ch["title"]))

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

        css_list = list(all_css_urls)
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
        # Deliberately done AFTER every O'Reilly request has completed. A local
        # LLM takes minutes per chapter, so translating inside the fetch loop
        # stretched the download over hours — long enough for the session to
        # expire, at which point O'Reilly answers 200 with a short preview stub
        # and the book silently loses all but the first paragraph per chapter.
        # Fetching everything first keeps all network I/O inside one fresh
        # session; the slow part now runs with no session dependency at all.
        if translate_enabled:
            translated_data = []
            for i, (filename, title, processed) in enumerate(chapters_data):
                if check_cancel():
                    self._cleanup_on_cancel(book_dir)
                    raise Exception("Download cancelled by user")

                pct = 90 + int((i / len(chapters_data)) * 5) if chapters_data else 90
                report(
                    "translating_chapters",
                    pct,
                    message=f"Traduciendo capítulo {i + 1}/{len(chapters_data)}",
                    current_chapter=i + 1,
                    total_chapters=len(chapters_data),
                    chapter_title=title,
                )

                translated = translator.translate_html(processed, target_lang)
                translated_data.append((filename, title, translated))

                # Re-write the on-disk XHTML so EPUB/PDF pick up the translation.
                xhtml_name, path_prefix, ch_title = chapter_files[i]
                css_refs = [
                    f"{path_prefix}Styles/Style{j:02d}.css" for j in range(len(css_list))
                ]
                xhtml = html_processor.wrap_xhtml(translated, css_refs, ch_title)
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

        # EPUB
        if "epub" in formats:
            def _make_epub():
                report("generating_epub", 90)
                epub_plugin = self.kernel["epub"]
                epub_path = epub_plugin.generate(
                    book_info=book_info,
                    chapters=chapters,
                    toc=toc,
                    output_dir=book_dir,
                    css_files=css_list,
                    cover_image="cover.jpg",
                )
                result.files["epub"] = str(epub_path)
            guard("epub", _make_epub)

        # Markdown
        if "markdown" in formats or "md" in formats or "markdown-chapters" in formats:
            def _make_markdown():
                report("generating_markdown", 92)
                md_plugin = self.kernel["markdown"]
                md_plugin.generate_book(book_info, chapters_data, book_dir)
                result.files["markdown"] = str(book_dir / "Markdown")
            guard("markdown", _make_markdown)

        # PDF
        if "pdf" in formats or "all" in formats or "pdf-chapters" in formats:
            def _make_pdf():
                pdf_plugin = self.kernel["pdf"]

                if "pdf-chapters" in formats:
                    report("generating_pdf_chapters", 95)
                    pdf_paths = pdf_plugin.generate_chapters(
                        book_info=book_info,
                        chapters=chapters,
                        output_dir=book_dir,
                        css_files=css_list,
                    )
                    result.files["pdf"] = [str(p) for p in pdf_paths]
                else:
                    report("generating_pdf", 95)
                    pdf_path = pdf_plugin.generate(
                        book_info=book_info,
                        chapters=chapters,
                        toc=toc,
                        output_dir=book_dir,
                        css_files=css_list,
                        cover_image="cover.jpg",
                    )
                    result.files["pdf"] = str(pdf_path)
            guard("pdf", _make_pdf)

        # Plain text
        if "plaintext" in formats or "txt" in formats or "plaintext-chapters" in formats:
            def _make_plaintext():
                report("generating_plaintext", 96)
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

        # JSON
        if "json" in formats:
            def _make_json():
                report("generating_json", 97)
                json_plugin = self.kernel["json_export"]
                json_path = json_plugin.generate(
                    book_dir=book_dir,
                    book_metadata=book_info,
                    chapters_data=chapters_data,
                    include_jsonl="jsonl" in formats,
                )
                result.files["json"] = str(json_path)
            guard("json", _make_json)

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
                    library.rebuild_index()
                    result.output_dir = Path(moved["path"])
                    result.files["library_rel"] = moved["rel"]
                guard("transfer", publish)

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

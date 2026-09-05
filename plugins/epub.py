import html
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .base import Plugin
from utils import sanitize_filename, slugify, write_text_utf8


class EpubPlugin(Plugin):
    def generate(
        self,
        book_info: dict,
        chapters: list[dict],
        toc: list[dict],
        output_dir: Path,
        css_files: list[str],
        cover_image: str | None = None,
        language: str | None = None,
    ) -> Path:
        oebps = output_dir / "OEBPS"
        oebps.mkdir(parents=True, exist_ok=True)
        (output_dir / "META-INF").mkdir(exist_ok=True)

        self._write_mimetype(output_dir)
        self._write_container_xml(output_dir)
        self._write_content_opf(oebps, book_info, chapters, css_files, cover_image,
                                language)
        self._write_toc_ncx(oebps, book_info, toc, language)
        self._write_nav_xhtml(oebps, book_info, toc, language)

        # Use sanitized title for epub filename
        epub_name = sanitize_filename(book_info.get("title", book_info["id"]))
        epub_path = output_dir / f"{epub_name}.epub"
        self._create_epub_zip(output_dir, epub_path)

        # Clean up build artifacts
        self._cleanup_build_artifacts(output_dir)

        return epub_path

    def _cleanup_build_artifacts(self, output_dir: Path):
        """Remove intermediate EPUB build files after ZIP creation."""
        artifacts = [
            output_dir / "mimetype",
            output_dir / "META-INF",
            output_dir / "OEBPS",
        ]
        for artifact in artifacts:
            if artifact.is_file():
                artifact.unlink()
            elif artifact.is_dir():
                shutil.rmtree(artifact)

    def _write_mimetype(self, output_dir: Path):
        write_text_utf8(output_dir / "mimetype", "application/epub+zip")

    def _write_container_xml(self, output_dir: Path):
        content = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''
        write_text_utf8(output_dir / "META-INF" / "container.xml", content)

    def _write_content_opf(
        self,
        oebps: Path,
        book_info: dict,
        chapters: list[dict],
        css_files: list[str],
        cover_image: str | None,
        language: str | None = None,
    ):
        title = html.escape(book_info.get("title", "Unknown"))
        authors = book_info.get("authors", [])
        isbn = book_info.get("isbn", book_info.get("id", "unknown"))
        description = html.escape(book_info.get("description", "")[:500])
        publishers = book_info.get("publishers", [])
        # El idioma del CONTENIDO, que no es el del catalogo cuando se ha
        # traducido. La especificacion exige al menos un dc:language, y un EPUB
        # en espanol que se declara ingles miente al lector: silabeo, glifos
        # del idioma y voz del lector de pantalla cuelgan de aqui.
        language = language or book_info.get("language", "en")
        pub_date = book_info.get("publication_date", "")

        author_xml = ""
        for author in authors:
            author_xml += f'    <dc:creator>{html.escape(author)}</dc:creator>\n'

        publisher_xml = ""
        for pub in publishers:
            publisher_xml += f"    <dc:publisher>{html.escape(pub)}</dc:publisher>\n"

        manifest_items = [
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        ]

        for i, ch in enumerate(chapters):
            filename = ch["filename"].replace(".html", ".xhtml")
            item_id = f"ch{i:03d}"
            manifest_items.append(
                f'    <item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>'
            )

        for i, css in enumerate(css_files):
            manifest_items.append(
                f'    <item id="css{i:02d}" href="Styles/Style{i:02d}.css" media-type="text/css"/>'
            )

        # El manifiesto tiene que declarar TODOS los recursos del paquete.
        # Antes solo recorria Images/, asi que las imagenes que un CSS
        # referencia con url(...) —que se descargan junto a la hoja de estilos,
        # dentro de Styles/— quedaban sin declarar. Un lector no puede resolver
        # lo que no esta en el manifiesto: epub.js acaba pidiendolas al servidor
        # y salen rotas.
        declared = {"toc.ncx", "nav.xhtml"}
        declared.update(f"Styles/Style{i:02d}.css" for i in range(len(css_files)))
        declared.update(
            ch["filename"].replace(".html", ".xhtml") for ch in chapters
        )

        for asset in sorted(a for a in oebps.rglob("*") if a.is_file()):
            rel = asset.relative_to(oebps).as_posix()
            if rel in declared:
                continue
            if asset.suffix.lower() in (".xhtml", ".html", ".css", ".opf", ".ncx"):
                continue

            # Id unico a partir de la ruta relativa y no del nombre: dos
            # archivos homonimos en carpetas distintas (Images/logo.png y
            # Styles/logo.png) generarian el mismo id y el OPF seria invalido.
            item_id = "res_" + re.sub(r"[^A-Za-z0-9]+", "_", rel).strip("_")
            properties = ""
            if cover_image and rel == f"Images/{cover_image}":
                properties = ' properties="cover-image"'
            manifest_items.append(
                f'    <item id="{item_id}" href="{rel}" '
                f'media-type="{self._get_image_media_type(asset.suffix)}"{properties}/>'
            )

        spine_items = []
        for i, ch in enumerate(chapters):
            spine_items.append(f'    <itemref idref="ch{i:03d}"/>')

        modified_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        content = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0" xml:lang="{language}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
    <dc:title>{title}</dc:title>
{author_xml}{publisher_xml}    <dc:description>{description}</dc:description>
    <dc:language>{language}</dc:language>
    <dc:identifier id="bookid">{isbn}</dc:identifier>
    <dc:date>{pub_date}</dc:date>
    <meta property="dcterms:modified">{modified_timestamp}</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine_items)}
  </spine>
</package>'''

        write_text_utf8(oebps / "content.opf", content)

    def _write_toc_ncx(self, oebps: Path, book_info: dict, toc: list[dict],
                       language: str | None = None):
        language = language or book_info.get("language", "en")
        title = html.escape(book_info.get("title", "Unknown"))
        isbn = book_info.get("isbn", book_info.get("id", "unknown"))
        authors = ", ".join(book_info.get("authors", ["Unknown"]))

        max_depth = self._get_max_depth(toc)
        nav_points, _ = self._build_nav_points(toc, 1)

        content = f'''<?xml version="1.0" encoding="utf-8" standalone="no"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta content="ID:ISBN:{isbn}" name="dtb:uid"/>
    <meta content="{max_depth}" name="dtb:depth"/>
    <meta content="{language}" name="dtb:language"/>
    <meta content="0" name="dtb:totalPageCount"/>
    <meta content="0" name="dtb:maxPageNumber"/>
  </head>
  <docTitle>
    <text>{title}</text>
  </docTitle>
  <docAuthor>
    <text>{html.escape(authors)}</text>
  </docAuthor>
  <navMap>
{nav_points}
  </navMap>
</ncx>'''

        write_text_utf8(oebps / "toc.ncx", content)

    # La cabecera del nav no es contenido del libro, es nuestra. Un mapa y no
    # una llamada al modelo: tres palabras deterministas valen mas que tres
    # palabras traducidas de una en una y sin contexto.
    NAV_HEADING = {
        "en": "Table of Contents",
        "es": "Tabla de contenidos",
    }

    def _write_nav_xhtml(self, oebps: Path, book_info: dict, toc: list[dict],
                         language: str | None = None):
        """Generate EPUB 3 navigation document (nav.xhtml)."""
        language = language or book_info.get("language", "en")
        nav_heading = self.NAV_HEADING.get(
            str(language).split("-")[0].lower(), self.NAV_HEADING["en"]
        )
        title = html.escape(book_info.get("title", "Unknown"))
        nav_items = self._build_nav_ol(toc)

        content = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{language}" xml:lang="{language}">
<head>
  <title>{title}</title>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>{nav_heading}</h1>
    <ol>
{nav_items}
    </ol>
  </nav>
</body>
</html>'''

        write_text_utf8(oebps / "nav.xhtml", content)

    def _build_nav_points(self, toc_items: list[dict], play_order: int, indent: int = 4) -> tuple[str, int]:
        result = []
        spaces = " " * indent

        for item in toc_items:
            nav_id = item.get("fragment") or item.get("ourn", "").split(":")[-1].replace(".html", "")
            label = html.escape(item.get("title", ""))
            href = item.get("reference_id", "").split("-/")[-1] if item.get("reference_id") else ""
            href = href.replace(".html", ".xhtml")

            if item.get("fragment"):
                href = f"{href}#{item['fragment']}"

            result.append(f'{spaces}<navPoint id="{nav_id}" playOrder="{play_order}">')
            result.append(f'{spaces}  <navLabel><text>{label}</text></navLabel>')
            result.append(f'{spaces}  <content src="{href}"/>')

            play_order += 1

            children = item.get("children", [])
            if children:
                child_points, play_order = self._build_nav_points(children, play_order, indent + 2)
                result.append(child_points)

            result.append(f'{spaces}</navPoint>')

        return "\n".join(result), play_order

    def _build_nav_ol(self, toc_items: list[dict], indent: int = 6) -> str:
        """Build ordered list items for nav.xhtml navigation (EPUB 3)."""
        result = []
        spaces = " " * indent

        for item in toc_items:
            label = html.escape(item.get("title", ""))
            href = item.get("reference_id", "").split("-/")[-1] if item.get("reference_id") else ""
            href = href.replace(".html", ".xhtml")

            if item.get("fragment"):
                href = f"{href}#{item['fragment']}"

            children = item.get("children", [])
            if children:
                child_ol = self._build_nav_ol(children, indent + 2)
                result.append(f'{spaces}<li>')
                result.append(f'{spaces}  <a href="{href}">{label}</a>')
                result.append(f'{spaces}  <ol>')
                result.append(child_ol)
                result.append(f'{spaces}  </ol>')
                result.append(f'{spaces}</li>')
            else:
                result.append(f'{spaces}<li><a href="{href}">{label}</a></li>')

        return "\n".join(result)

    def _get_max_depth(self, toc_items: list[dict], current: int = 1) -> int:
        max_d = current
        for item in toc_items:
            children = item.get("children", [])
            if children:
                max_d = max(max_d, self._get_max_depth(children, current + 1))
        return max_d

    def _get_image_media_type(self, suffix: str) -> str:
        types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".ttf": "font/ttf",
            ".otf": "font/otf",
            ".eot": "application/vnd.ms-fontobject",
        }
        return types.get(suffix.lower(), "application/octet-stream")

    def _create_epub_zip(self, output_dir: Path, epub_path: Path):
        with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as zf:
            mimetype_path = output_dir / "mimetype"
            zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)

            for file_path in output_dir.rglob("*"):
                if file_path.is_file() and file_path.name != "mimetype":
                    arcname = file_path.relative_to(output_dir)
                    if not str(arcname).endswith(".epub"):
                        zf.write(file_path, arcname)

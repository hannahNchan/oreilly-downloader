"""Audiobook plugin: search and download O'Reilly audiobooks.

Audiobooks do NOT go through the EPUB pipeline. They live on the video
infrastructure and their audio is served by Kaltura, so the chain is:

  1. /search/api/search/?type=audiobook   -> product_id (sufijo "AU")
  2. /api/v1/videoplaylists/{id}/         -> metadata + clips + spine
  3. /api/v1/videoclips/{clip_ref}/       -> kaltura_entry_id por capitulo
  4. /api/v1/player/kaltura_config/       -> partner_id
  5. /api/v1/player/kaltura_session/      -> token KS (caduca en horas)
  6. cdnapisec.kaltura.com/.../a.m4a      -> el audio

El token KS se pide de nuevo por capitulo: una descarga de ~10 h puede durar
mas que su vigencia, y renovarlo cuesta una peticion trivial.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

# curl_cffi ya es dependencia del proyecto (lo usa HttpClient).
from curl_cffi import requests

import config
from utils import sanitize_filename, slugify

from .base import Plugin

SEARCH_URL = "https://learning.oreilly.com/search/api/search/"
API_V1 = "https://learning.oreilly.com/api/v1"
KALTURA_CDN = "https://cdnapisec.kaltura.com"

# Trozo de lectura al escribir a disco: grande para no hacer miles de
# iteraciones, chico para poder cancelar con rapidez.
CHUNK_BYTES = 1 << 20  # 1 MiB


def _as_int(value):
    """Numero o None. La API mezcla enteros y cadenas en los mismos campos."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass
class AudiobookChapter:
    """Un capitulo de audiolibro, ya resuelto a su entry de Kaltura."""

    index: int
    title: str
    reference_id: str
    entry_id: str
    duration: int = 0


@dataclass
class AudiobookResult:
    """Resultado de una descarga de audiolibro."""

    book_id: str
    title: str
    output_dir: Path
    files: list = field(default_factory=list)
    errors: dict = field(default_factory=dict)
    chapters_count: int = 0


class AudiobookPlugin(Plugin):
    """Busca y descarga audiolibros como archivos .m4a por capitulo."""

    def search(
        self,
        query: str,
        limit: int = 25,
        page: int = 0,
        language: str | None = None,
    ) -> dict:
        """Busca audiolibros. `page` es 0-based aqui y 1-based en la API."""
        params = [
            f"q={quote_plus(query)}",
            f"rows={limit}",
            f"page={page + 1}",  # este endpoint pagina desde 1
            "type=audiobook",
            "tzOffset=0",
            "aia_only=false",
            "report=true",
            "isTopics=false",
        ]
        if language:
            params.append(f"language={quote_plus(language)}")

        data = self.http.get_json(f"{SEARCH_URL}?" + "&".join(params)).get("data", {})
        total = data.get("total", 0)

        results = []
        for p in data.get("products", []):
            ca = p.get("custom_attributes", {}) or {}
            results.append({
                "id": p.get("product_id"),
                "title": p.get("title"),
                "authors": p.get("authors", []),
                "cover_url": p.get("cover_image"),
                "publishers": ca.get("publishers", []),
                "duration_seconds": ca.get("duration_seconds"),
                "publication_date": ca.get("publication_date"),
                "content_type": "audiobook",
            })

        return {
            "results": results,
            "page": page,
            "limit": limit,
            "total": total,
            "has_more": (page + 1) * limit < total,
        }

    def fetch(self, book_id: str) -> dict:
        """Metadata del audiolibro (titulo, autores, duracion, portada)."""
        pl = self.http.get_json(f"{API_V1}/videoplaylists/{book_id}/")
        contributors = pl.get("contributors") or {}
        publisher = pl.get("publisher") or {}
        return {
            "id": book_id,
            "title": pl.get("title", ""),
            "authors": contributors.get("authors", []),
            "publishers": [publisher["name"]] if publisher.get("name") else [],
            "publication_date": pl.get("publication_date"),
            "duration_seconds": pl.get("total_running_time_secs"),
            "cover_url": f"https://learning.oreilly.com/covers/{book_id}/",
            "language": pl.get("language"),
            "content_type": "audiobook",
            "chapters_count": len(pl.get("video_clips") or []),
        }

    def fetch_chapters(self, book_id: str) -> list[AudiobookChapter]:
        """Resuelve cada clip del spine a su entry de Kaltura, en orden."""
        pl = self.http.get_json(f"{API_V1}/videoplaylists/{book_id}/")
        chapters: list[AudiobookChapter] = []

        for i, clip_url in enumerate(pl.get("video_clips") or []):
            clip = self.http.get_json(clip_url)
            entry = clip.get("kaltura_entry_id")
            if not entry:
                continue  # sin entry no hay audio que bajar
            chapters.append(AudiobookChapter(
                index=i,
                title=clip.get("title") or f"Chapter {i + 1}",
                reference_id=clip.get("reference_id", ""),
                entry_id=entry,
                duration=int(clip.get("duration") or 0),
            ))

        return chapters

    # --- Kaltura ---------------------------------------------------------

    def _partner_id(self) -> str:
        cfg = self.http.get_json(f"{API_V1}/player/kaltura_config/")
        return str(cfg["partner_id"])

    def _kaltura_session(self, entry_id: str) -> str:
        url = f"{API_V1}/player/kaltura_session/?entry_id={entry_id}"
        return self.http.get_json(url)["session"]

    def _audio_url(self, partner_id: str, entry_id: str, ks: str) -> str:
        return (
            f"{KALTURA_CDN}/p/{partner_id}/sp/{partner_id}00/playManifest"
            f"/entryId/{entry_id}/format/download/protocol/https/ks/{ks}/a.m4a"
        )

    def _download_audio(
        self,
        url: str,
        dest: Path,
        cancel_check: Callable[[], bool] | None = None,
        on_bytes: Callable[[int, int], None] | None = None,
    ) -> int:
        """Baja un .m4a a disco en trozos.

        Se escribe a un `.part` y se renombra al final, para que una descarga
        interrumpida no quede como un archivo aparentemente valido.
        Kaltura es un CDN de terceros: NO se le mandan las cookies de O'Reilly,
        por eso se usa `requests` directo y no self.http.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")

        resp = requests.get(url, stream=True, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)

        written = 0
        try:
            with open(partial, "wb") as handle:
                for chunk in resp.iter_content(CHUNK_BYTES):
                    if cancel_check and cancel_check():
                        raise Exception("Download cancelled by user")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if on_bytes:
                        on_bytes(written, total)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        partial.replace(dest)
        return written

    @staticmethod
    def _write_sidecar(book_dir: Path, info: dict, chapters=None) -> None:
        """Guarda la metadata del audiolibro para el visor local.

        Incluye los titulos de los capitulos: los archivos se renombran a
        001.m4a al publicar, asi que si no se guardan aqui el reproductor solo
        puede mostrar numeros.
        """
        date = info.get("publication_date") or ""
        payload = {
            "title": info.get("title"),
            "authors": info.get("authors") or [],
            "publishers": info.get("publishers") or [],
            "language": info.get("language"),
            "date": date or None,
            "year": date[:4] if len(date) >= 4 and date[:4].isdigit() else None,
            "isbn": info.get("id"),
            "content_type": "audiobook",
            # La API lo devuelve como texto; se guarda numerico para poder
            # comparar contra lo que hay realmente en disco.
            "duration_seconds": _as_int(info.get("duration_seconds")),
            # Cuantas pistas DEBERIA tener. Sin esto no habia forma de saber
            # que una descarga cortada quedo a medias.
            "expected_tracks": len(chapters or []),
            "chapters": [
                {"n": n, "title": ch.title, "duration": ch.duration}
                for n, ch in enumerate(chapters or [], start=1)
            ],
        }
        (book_dir / "library.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _save_cover(self, book_dir: Path, book_id: str) -> bool:
        """Guarda la portada como cover.jpg para el visor local.

        Un audiolibro no trae epub del que extraerla, asi que se baja una vez
        aqui; si no, el visor tendria que pedirla a O'Reilly en cada carga.
        """
        target = book_dir / "cover.jpg"
        if target.exists():
            return True
        try:
            data = self.http.get_bytes(
                f"https://learning.oreilly.com/library/cover/{book_id}/"
            )
            if data:
                target.write_bytes(data)
                return True
        except Exception:
            pass  # sin portada el visor cae al placeholder
        return False

    # --- orquestacion ----------------------------------------------------

    def download(
        self,
        book_id: str,
        output_dir: Path,
        selected_chapters: list[int] | None = None,
        progress_callback: Callable[..., None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        transfer: bool = True,
    ) -> AudiobookResult:
        """Descarga un audiolibro como un .m4a por capitulo."""
        info = self.fetch(book_id)
        all_chapters = self.fetch_chapters(book_id)

        if selected_chapters is not None:
            wanted = set(selected_chapters)
            chapters = [c for c in all_chapters if c.index in wanted]
        else:
            chapters = all_chapters

        # Sufijo "-audiobook": el libro y su audiolibro comparten titulo, asi
        # que sin el caerian en la misma carpeta y el .book_id del audiolibro
        # sobrescribiria el del EPUB (rompiendo el cintillo "En la biblioteca").
        book_dir = output_dir / f"{slugify(info.get('title') or book_id)}-audiobook"
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / ".book_id").write_text(book_id, encoding="utf-8")

        # Sidecar de metadata: los .m4a no llevan etiquetas, asi que sin esto
        # el visor de biblioteca no tendria titulo, autor, idioma ni año.
        self._write_sidecar(book_dir, info, chapters)
        self._save_cover(book_dir, book_id)

        result = AudiobookResult(
            book_id=book_id,
            title=info.get("title", ""),
            output_dir=book_dir,
            chapters_count=len(chapters),
        )

        def report(**kwargs):
            if progress_callback:
                progress_callback(**kwargs)

        partner_id = self._partner_id()
        total_ch = len(chapters)

        for n, ch in enumerate(chapters):
            if cancel_check and cancel_check():
                raise Exception("Download cancelled by user")

            report(
                status="downloading_audio",
                percentage=int((n / total_ch) * 100) if total_ch else 0,
                current_chapter=n + 1,
                total_chapters=total_ch,
                chapter_title=ch.title,
                message=f"Descargando {n + 1}/{total_ch}: {ch.title}",
            )

            # Nombre ordenable y legible en cualquier reproductor
            name = f"{ch.index + 1:02d} - {sanitize_filename(ch.title)}.m4a"
            dest = book_dir / name

            if dest.exists():
                result.files.append(str(dest))
                continue  # ya estaba: no se vuelve a bajar

            try:
                # KS nuevo por capitulo: el token caduca en horas y una
                # descarga completa puede durar mas que eso.
                ks = self._kaltura_session(ch.entry_id)
                url = self._audio_url(partner_id, ch.entry_id, ks)

                def on_bytes(done: int, size: int, _n=n, _ch=ch):
                    if not size:
                        return
                    inner = done / size
                    report(
                        status="downloading_audio",
                        percentage=int(((_n + inner) / total_ch) * 100),
                        current_chapter=_n + 1,
                        total_chapters=total_ch,
                        chapter_title=_ch.title,
                        message=(
                            f"Descargando {_n + 1}/{total_ch}: {_ch.title} "
                            f"({done >> 20}/{size >> 20} MB)"
                        ),
                    )

                self._download_audio(url, dest, cancel_check, on_bytes)
                result.files.append(str(dest))
            except Exception as exc:
                if "cancel" in str(exc).lower():
                    raise
                result.errors[ch.title] = f"{type(exc).__name__}: {exc}"

        # Forma canonica en la cache local y, si se pidio, publicacion. Un fallo
        # aqui no cuesta la descarga: la obra sigue en cache y se puede pasar
        # despues desde el visor.
        library = self.kernel.get("library")
        if library is not None:
            library.finalize_object(
                book_dir,
                info | {"content_type": "audiobook", "book_id": book_id},
                rebuild=False,
            )
            if transfer and library.ensure_root():
                report(status="transferring", percentage=97,
                       message="Pasando a la biblioteca...")
                moved = library.transfer_object(book_dir, book_id, "audiobook")
                library.rebuild_index()
                result.output_dir = Path(moved["path"])

        report(status="completed", percentage=100, message="Audiolibro descargado")
        return result

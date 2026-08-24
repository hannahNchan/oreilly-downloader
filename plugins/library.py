"""Library plugin: indexes what has already been downloaded to disk.

The output folder keeps almost no metadata: only `.book_id` and the files
themselves. To make a local library browsable (filter by author, publisher,
language, year) the metadata has to come from somewhere, so:

- Books: read `content.opf` from inside the .epub. Works fully offline.
- Audiobooks: .m4a files carry no tags, so a `library.json` sidecar written at
  download time is the source. Folders without one degrade to name + files.

Unzipping every epub on each request would be wasteful, so the result is cached
in `<output>/.library-index.json` and only folders whose mtime changed are
re-read.
"""

import hashlib
import html
import json
import re
import shutil
import zipfile
from pathlib import Path

import config

from .base import Plugin

INDEX_FILE = ".library-index.json"
SIDECAR_FILE = "library.json"
COVER_FILE = "cover.jpg"

# Nombres habituales de la portada dentro del epub, en orden de preferencia
COVER_PATTERNS = ("cover.jpg", "cover.jpeg", "cover.png")

# Nombre canonico dentro del objeto, por extension
CANONICAL_NAME = {
    ".epub": "book.epub",
    ".pdf": "book.pdf",
    ".azw3": "book.azw3",
    ".txt": "book.txt",
    ".toon": "book.toon",
}
AUDIO_SUFFIXES = (".m4a", ".mp3")
# Metadata nuestra: no cuenta como formato ni suma al tamanio del contenido
NOT_CONTENT = {"metadata.json", COVER_FILE, SIDECAR_FILE, "cover.webp"}

# Extensiones que cuentan como "formato disponible" en disco
FORMAT_BY_SUFFIX = {
    ".epub": "epub",
    ".pdf": "pdf",
    ".m4a": "audio",
    ".md": "markdown",
    ".json": "json",
    ".jsonl": "jsonl",
    ".txt": "text",
    ".toon": "toon",
}


class LibraryPlugin(Plugin):
    """Indexa y filtra la biblioteca local (libros y audiolibros)."""

    # --- lectura de metadata ---------------------------------------------

    @staticmethod
    def _read_epub_metadata(epub_path: Path) -> dict:
        """Extrae metadata del content.opf que va dentro del .epub."""
        try:
            with zipfile.ZipFile(epub_path) as zf:
                name = next(
                    (n for n in zf.namelist() if n.endswith("content.opf")), None
                )
                if not name:
                    return {}
                xml = zf.read(name).decode("utf-8", "ignore")
        except (zipfile.BadZipFile, OSError):
            return {}  # epub corrupto o a medio escribir

        def values(tag: str) -> list[str]:
            found = re.findall(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", xml, re.S)
            out = []
            for raw in found:
                # El opf trae entidades (&#x27;) y puede llevar markup dentro
                text = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
                if text:
                    out.append(text)
            return out

        first = lambda tag: (values(tag) or [None])[0]  # noqa: E731
        date = first("date") or ""

        return {
            "title": first("title"),
            "authors": values("creator"),
            "publishers": values("publisher"),
            "language": first("language"),
            "date": date or None,
            "year": date[:4] if len(date) >= 4 and date[:4].isdigit() else None,
            "isbn": first("identifier"),
        }

    @staticmethod
    def _extract_cover(epub_path: Path, folder: Path) -> bool:
        """Saca la portada del epub a `cover.jpg` dentro de la carpeta.

        El epub ya trae OEBPS/Images/cover.jpg, asi que la portada del visor
        local no necesita red ni sesion de O'Reilly. Se hace una sola vez: si
        el archivo ya existe no se vuelve a abrir el zip.
        """
        target = folder / COVER_FILE
        if target.exists():
            return True
        try:
            with zipfile.ZipFile(epub_path) as zf:
                names = zf.namelist()
                pick = None
                for pattern in COVER_PATTERNS:
                    pick = next(
                        (n for n in names if n.lower().endswith("/" + pattern)
                         or n.lower() == pattern),
                        None,
                    )
                    if pick:
                        break
                if not pick:
                    return False
                target.write_bytes(zf.read(pick))
            return True
        except (zipfile.BadZipFile, OSError, KeyError):
            return False

    @classmethod
    def _ensure_cover(cls, obj: Path) -> bool:
        """Garantiza <obj>/cover.jpg. True si existe al salir.

        Orden: la que ya esta en disco, la que dejo la descarga en
        OEBPS/Images/, y por ultimo la que va dentro del epub. Ese ultimo caso
        es el normal: el empaquetador borra OEBPS despues de comprimir, asi que
        para cuando se finaliza el objeto la portada solo sobrevive en el epub.
        """
        target = obj / COVER_FILE
        if target.is_file() and target.stat().st_size:
            return True

        staged = obj / "OEBPS" / "Images" / COVER_FILE
        if staged.is_file():
            try:
                shutil.copy2(staged, target)
                return True
            except OSError:
                pass  # se intenta con el epub

        # Un solo nombre (cover.jpg) aunque los bytes sean PNG: el endpoint
        # sirve un nombre fijo y el <img> decodifica por contenido.
        for epub in sorted(obj.glob("*.epub")):
            if cls._extract_cover(epub, obj):
                return True
        return False

    @staticmethod
    def _read_sidecar(folder: Path) -> dict:
        try:
            return json.loads((folder / SIDECAR_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # --- biblioteca publicada: escritura ----------------------------------

    @staticmethod
    def work_id_for(book_id: str, content_type: str) -> tuple[str, str]:
        """(work_id, shard) derivado del ourn.

        Determinista: descargar dos veces el mismo libro reusa el mismo objeto
        en vez de crear un duplicado.
        """
        kind = "audiobook" if content_type == "audiobook" else "book"
        digest = hashlib.sha1(f"urn:orm:{kind}:{book_id}".encode()).hexdigest()
        return digest[:8], digest[:2]

    def finalize_object(self, obj: Path, meta: dict, rebuild: bool = True) -> None:
        """Deja el objeto en forma canonica tras una descarga.

        Renombra a book.epub / audiobook/001.m4a y escribe metadata.json. Se
        ejecuta al terminar la descarga, cuando ya estan todos los formatos.

        `rebuild=False` para el staging local: ahi no hay indice que refrescar,
        y la obra queda ya con la forma exacta que tendra en la biblioteca, asi
        que pasarla es una copia y nada mas.
        """
        if not obj.is_dir():
            return

        # Pistas de audio: renumeradas por orden alfabetico del nombre original
        audio = sorted(p for p in obj.iterdir()
                       if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)
        for n, src in enumerate(audio, start=1):
            dest = obj / "audiobook" / f"{n:03d}{src.suffix.lower()}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src != dest:
                src.replace(dest)

        for src in list(obj.iterdir()):
            if not src.is_file() or src.name in NOT_CONTENT:
                continue
            canonical = CANONICAL_NAME.get(src.suffix.lower())
            # Si el nombre canonico ya esta ocupado se deja como esta: dos .txt
            # renombrados al mismo destino harian que el segundo borrase al
            # primero en silencio.
            if canonical and src.name != canonical and not (obj / canonical).exists():
                src.replace(obj / canonical)

        # La portada tiene que quedar en la raiz del objeto: es de ahi de donde
        # rebuild_index alimenta covers/, y sin ella la tarjeta sale vacia.
        self._ensure_cover(obj)

        # El work_id se deriva del ourn, NUNCA del nombre de la carpeta: en el
        # staging local la carpeta es el slug del titulo, y ese nombre viajaria
        # a la biblioteca dentro del metadata.json, sin portada que resolver.
        book_id = meta.get("book_id") or ""
        content_type = meta.get("content_type") or "book"
        wid = self.work_id_for(book_id, content_type)[0] if book_id else obj.name
        payload = {
            "work_id": wid,
            "ourn": f"urn:orm:{'audiobook' if content_type == 'audiobook' else 'book'}"
                    f":{book_id}",
            "book_id": book_id,
            "content_type": content_type,
            "title": meta.get("title") or wid,
            "authors": meta.get("authors") or [],
            "publishers": meta.get("publishers") or [],
            "language": meta.get("language"),
            "year": (meta.get("date") or "")[:4] or meta.get("year"),
            "date": meta.get("date"),
            "isbn": meta.get("isbn") or meta.get("book_id"),
        }
        (obj / "metadata.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

        if rebuild:
            self.rebuild_index()

    def transfer_object(
        self,
        local_dir: Path,
        book_id: str,
        content_type: str = "book",
        on_progress=None,
        remove_source: bool = True,
    ) -> dict:
        """Publica en la biblioteca una obra ya terminada en la cache local.

        Se copia a `<work_id>.part` y solo al final se renombra al nombre
        definitivo: si la red se cae a media copia no queda media obra visible
        en la biblioteca. No reconstruye el indice — eso lo hace quien llama,
        una vez por cola en vez de una por obra.
        """
        root = self.ensure_root()
        if root is None:
            raise RuntimeError(
                f"La carpeta de la biblioteca no esta accesible: {config.LIBRARY_DIR}")
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise FileNotFoundError(f"No existe el origen: {local_dir}")

        # Se publica en forma canonica: si el origen viene de una descarga
        # antigua sin metadata.json, se reconstruye ANTES de copiar. Publicar una
        # obra que el indice no puede leer es lo mismo que perderla de vista.
        if not (local_dir / "metadata.json").is_file():
            self._recover_object(local_dir)

        wid, shard = self.work_id_for(book_id, content_type)
        dest = root / "objects" / shard / wid
        staging = dest.parent / f"{wid}.part"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        expected = {
            p.relative_to(local_dir).as_posix(): p.stat().st_size
            for p in local_dir.rglob("*") if p.is_file()
        }
        total = sum(expected.values()) or 1
        copied_bytes = 0
        for rel, size in expected.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_dir / rel, target)
            copied_bytes += size
            if on_progress:
                on_progress(file=rel, copied=copied_bytes, total=total)

        # Verificacion antes de publicar: mismo conjunto de archivos y mismos
        # tamanios. Sin esto una copia truncada por un corte de red quedaria
        # publicada como si estuviera completa.
        got = {
            p.relative_to(staging).as_posix(): p.stat().st_size
            for p in staging.rglob("*") if p.is_file()
        }
        if got != expected:
            shutil.rmtree(staging, ignore_errors=True)
            missing = sorted(set(expected) - set(got))
            detail = f", falta {missing[0]}" if missing else ""
            raise RuntimeError(
                f"Transferencia incompleta: {len(got)}/{len(expected)} archivos{detail}"
            )

        # Publicacion. Si ya existia una version se aparta primero y se borra
        # despues del rename, para que la biblioteca nunca quede sin la obra.
        old = None
        if dest.exists():
            old = dest.parent / f"{wid}.old"
            shutil.rmtree(old, ignore_errors=True)
            dest.replace(old)
        staging.replace(dest)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)

        if remove_source:
            shutil.rmtree(local_dir, ignore_errors=True)

        return {
            "work_id": wid,
            "rel": f"objects/{shard}/{wid}",
            "path": str(dest),
            "files": len(expected),
            "bytes": sum(expected.values()),
        }

    def _recover_object(self, obj: Path) -> bool:
        """Rehace el metadata.json de una obra que no lo tiene. True si lo logra.

        Pasa con obras descargadas antes de que existiera finalize_object, o
        copiadas a mano de otra biblioteca. Sin esto quedaban INVISIBLES:
        rebuild_index las saltaba, asi que la obra estaba en disco y no aparecia
        en el visor. La metadata se deduce de lo que hay: el opf de dentro del
        epub, el sidecar del audiolibro y el marcador .book_id.
        """
        book_id = ""
        marker = obj / ".book_id"
        if marker.is_file():
            try:
                book_id = marker.read_text(encoding="utf-8").strip()
            except OSError:
                book_id = ""

        epubs = sorted(obj.glob("*.epub"))
        audio = [f for f in obj.rglob("*")
                 if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES]
        if not epubs and not audio:
            return False  # sin contenido no hay obra que indexar

        meta = self._read_epub_metadata(epubs[0]) if epubs else {}
        meta.update({k: v for k, v in self._read_sidecar(obj).items() if v})

        # finalize_object hace el resto: nombres canonicos, portada y el
        # metadata.json en si. Es el mismo camino que una descarga normal.
        self.finalize_object(obj, {
            "book_id": book_id,
            "content_type": "audiobook" if audio and not epubs else "book",
            "title": meta.get("title"),
            "authors": meta.get("authors"),
            "publishers": meta.get("publishers"),
            "language": meta.get("language"),
            "date": meta.get("date"),
            "year": meta.get("year"),
            "isbn": meta.get("isbn"),
        }, rebuild=False)
        return (obj / "metadata.json").is_file()

    def rebuild_index(self) -> int:
        """Regenera index/library.json y covers/ recorriendo objects/.

        El indice es DERIVADO: se reconstruye desde lo que hay realmente en
        disco, asi que nunca puede desincronizarse del almacen.
        """
        root = self.ensure_root()
        if root is None:
            return 0

        objects = root / "objects"
        if not objects.is_dir():
            return 0

        covers = root / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        items = []
        live: set[str] = set()

        for shard in sorted(objects.iterdir()):
            if not shard.is_dir():
                continue
            for obj in sorted(shard.iterdir()):
                # `.part` es una copia a medias y `.old` una version apartada:
                # ninguna de las dos es una obra publicada.
                if not obj.is_dir() or obj.suffix in (".part", ".old"):
                    continue
                meta_file = obj / "metadata.json"
                if not meta_file.is_file() and not self._recover_object(obj):
                    continue
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue

                files = [f for f in obj.rglob("*")
                         if f.is_file() and f.name not in NOT_CONTENT]
                meta["formats"] = sorted({
                    FORMAT_BY_SUFFIX[f.suffix.lower()]
                    for f in files if f.suffix.lower() in FORMAT_BY_SUFFIX
                })
                meta["size_bytes"] = sum(f.stat().st_size for f in files)
                meta["tracks"] = sum(1 for f in files
                                     if f.suffix.lower() in AUDIO_SUFFIXES)
                meta["rel"] = obj.relative_to(root).as_posix()
                # La carpeta ES la direccion del objeto: si el metadata.json
                # dice otra cosa, se equivoca el metadata.
                meta["work_id"] = obj.name
                items.append(meta)

                # Autoreparacion: un objeto sin portada la saca de su propio
                # epub, asi que lo ya descargado se arregla en el siguiente
                # indexado en vez de quedarse sin imagen para siempre.
                has_cover = self._ensure_cover(obj)
                meta["has_cover"] = has_cover
                src = obj / COVER_FILE
                dst = covers / f"{obj.name}.jpg"
                if has_cover:
                    live.add(dst.name)
                    if (not dst.exists()
                            or dst.stat().st_size != src.stat().st_size):
                        shutil.copy2(src, dst)

        # Portadas de objetos que ya no existen: sin esto se acumulan para
        # siempre en el recurso compartido.
        for stale in covers.glob("*.jpg"):
            if stale.name not in live:
                try:
                    stale.unlink()
                except OSError:
                    pass

        items.sort(key=lambda m: (m.get("title") or "").lower())
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "library.json").write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return len(items)

    # --- biblioteca publicada: lectura ------------------------------------

    @staticmethod
    def root() -> Path | None:
        """Raiz de la biblioteca si esta accesible, o None.

        None significa "configurada pero no alcanzable": una unidad de red sin
        montar, un disco desconectado. Quien llama debe avisar en vez de dar por
        hecho que la biblioteca esta vacia.
        """
        configured = getattr(config, "LIBRARY_DIR", None)
        if not configured:
            return None
        configured = Path(configured)
        return configured if configured.is_dir() else None

    @classmethod
    def ensure_root(cls) -> Path | None:
        """Como root(), pero creando la estructura si falta. None si no se puede.

        No se comprueba la ruta por adelantado: si la unidad no esta montada, el
        propio mkdir falla, y eso es mas fiable que adivinar si una ruta de red
        va a responder.
        """
        configured = getattr(config, "LIBRARY_DIR", None)
        if not configured:
            return None
        configured = Path(configured)
        try:
            for sub in ("objects", "index", "covers"):
                (configured / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            return None  # no montada, sin permisos, ruta invalida
        return configured

    def _load_index(self, root: Path) -> list[dict]:
        """Lee index/library.json y lo adapta a la forma que usa el visor."""
        try:
            data = json.loads(
                (root / "index" / "library.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return []

        items = []
        for entry in data.get("items", []):
            wid = entry.get("work_id") or ""
            rel = entry.get("rel") or ""
            bid = entry.get("book_id") or ""
            items.append({
                # `folder` es la clave de identidad que usa la UI (favoritos,
                # ocultos), asi que aqui pasa a ser el work_id.
                "folder": wid,
                "path": str(root / rel) if rel else "",
                "book_id": entry.get("book_id") or "",
                "content_type": entry.get("content_type") or "book",
                "title": entry.get("title") or wid,
                "authors": entry.get("authors") or [],
                "publishers": entry.get("publishers") or [],
                "language": entry.get("language"),
                "year": entry.get("year"),
                "date": entry.get("date"),
                "isbn": entry.get("isbn"),
                "formats": entry.get("formats") or [],
                "files_count": len(entry.get("formats") or []),
                "tracks": entry.get("tracks") or 0,
                "size_bytes": entry.get("size_bytes") or 0,
                # Solo se anuncia la portada local si esta de verdad en disco:
                # anunciarla siempre daba un 404 y el navegador pintaba el
                # icono de imagen rota. Sin ella se cae a la remota.
                "cover_url": (
                    f"/api/library/cover/{wid}"
                    if wid and entry.get("has_cover")
                    else (f"https://learning.oreilly.com/library/cover/{bid}/"
                          if bid else None)
                ),
                "cover_local": bool(entry.get("has_cover")),
                "related_ourn": entry.get("related_ourn"),
                "work_id": wid,
                "rel": rel,
                "mtime": 0,
            })
        return items

    # --- escaneo ---------------------------------------------------------

    def _scan_folder(self, folder: Path) -> dict | None:
        """Construye la entrada de índice de una carpeta, o None si está vacía."""
        # Recursivo: tras finalize_object las pistas viven en audiobook/. Se
        # excluyen los restos del armado del epub (OEBPS/, META-INF/), que son
        # intermedios de una descarga a medias y no contenido de la obra.
        files = [
            p for p in folder.rglob("*")
            if p.is_file() and not p.name.startswith(".")
            and p.name not in NOT_CONTENT
            and not {"OEBPS", "META-INF"} & set(p.relative_to(folder).parts)
        ]
        if not files and not (folder / SIDECAR_FILE).exists():
            return None

        formats = sorted({
            FORMAT_BY_SUFFIX[p.suffix.lower()]
            for p in files
            if p.suffix.lower() in FORMAT_BY_SUFFIX
        })
        total_bytes = sum(p.stat().st_size for p in files)

        audio_files = [p for p in files if p.suffix.lower() in AUDIO_SUFFIXES]
        epub_files = [p for p in files if p.suffix.lower() == ".epub"]
        # El tipo se decide por lo que hay en disco, no por el nombre de la
        # carpeta: una carpeta renombrada sigue clasificando bien.
        content_type = "audiobook" if audio_files and not epub_files else "book"

        book_id = ""
        marker = folder / ".book_id"
        if marker.exists():
            try:
                book_id = marker.read_text(encoding="utf-8").strip()
            except OSError:
                book_id = ""

        # El sidecar manda; el opf del epub rellena; el nombre de carpeta es
        # el último recurso para tener al menos un título mostrable.
        meta = {}
        if epub_files:
            meta = self._read_epub_metadata(epub_files[0])
        # metadata.json lo escribe finalize_object desde la API, asi que gana
        # al opf del epub; el sidecar del audiolibro es el ultimo en aplicarse.
        try:
            written = json.loads(
                (folder / "metadata.json").read_text(encoding="utf-8"))
            meta.update({k: v for k, v in written.items() if v})
        except (OSError, json.JSONDecodeError):
            pass
        meta.update({k: v for k, v in self._read_sidecar(folder).items() if v})

        # La portada se extrae una vez y queda en disco junto al libro
        has_cover = (folder / COVER_FILE).exists()
        if not has_cover and epub_files:
            has_cover = self._extract_cover(epub_files[0], folder)

        # Fallback: nombre de carpeta sin el sufijo de tipo. Sólo se usa si no
        # hay sidecar ni epub del que leer el título real.
        pretty = re.sub(r"-audiobook$", "", folder.name).replace("-", " ").title()
        title = meta.get("title") or pretty

        # Mismo work_id que tendria en la biblioteca: es la clave con la que se
        # deduplica una obra que esta en los dos lados.
        book_id = book_id or (meta.get("book_id") or "")
        wid = self.work_id_for(book_id, content_type)[0] if book_id else ""

        return {
            "folder": folder.name,
            "path": str(folder),
            "work_id": wid,
            "book_id": book_id,
            "content_type": content_type,
            "title": title,
            "authors": meta.get("authors") or [],
            "publishers": meta.get("publishers") or [],
            "language": meta.get("language"),
            "year": meta.get("year"),
            "date": meta.get("date"),
            "isbn": meta.get("isbn"),
            "formats": formats,
            "files_count": len(files),
            "tracks": len(audio_files),
            "size_bytes": total_bytes,
            # Portada local si se pudo extraer; si no, la remota como respaldo
            "cover_url": (
                f"/api/library/cover/{folder.name}"
                if has_cover
                else (f"https://learning.oreilly.com/library/cover/{book_id}/"
                      if book_id else None)
            ),
            "cover_local": has_cover,
            "mtime": int(folder.stat().st_mtime),
        }

    def scan(self, output_dir: Path | None = None, refresh: bool = False) -> list[dict]:
        """Índice fusionado: lo publicado en la biblioteca + la cache local.

        Cada obra lleva `location` para que el visor distinga lo ya publicado de
        lo que sigue esperando. Si la misma obra aparece en los dos lados manda
        la de la biblioteca, que es la canonica: la copia en cache solo sobra
        hasta que alguien la borre.
        """
        items: list[dict] = []
        seen: set[str] = set()

        published = self.root()
        if published is not None and output_dir is None:
            for entry in self._load_index(published):
                entry["location"] = "library"
                items.append(entry)
                if entry.get("work_id"):
                    seen.add(entry["work_id"])

        for entry in self._scan_local(output_dir, refresh):
            if entry.get("work_id") and entry["work_id"] in seen:
                continue
            entry["location"] = "local"
            items.append(entry)

        return items

    def _scan_local(self, output_dir: Path | None, refresh: bool) -> list[dict]:
        """Recorre el staging local, con caché por mtime de carpeta."""
        base = output_dir or self.kernel["output"].get_default_dir()
        if not base.exists():
            return []

        cached: dict[str, dict] = {}
        index_path = base / INDEX_FILE
        if not refresh and index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                cached = {it["folder"]: it for it in data.get("items", [])}
            except (OSError, json.JSONDecodeError):
                cached = {}  # caché ilegible: se reconstruye

        # La raiz de la biblioteca vive dentro de output por defecto
        # (output/library), asi que hay que saltarla explicitamente: si no, se
        # escanea a si misma y aparece como UNA obra gigante que suma otra vez
        # el tamaño de toda la biblioteca. Comparar por ruta resuelta y no por
        # nombre, porque el usuario puede llamarla como quiera.
        published = self.root()
        skip = {published.resolve()} if published is not None else set()
        # Y si la raiz es output mismo, estas tres son su estructura interna.
        reserved = {"objects", "index", "covers"}

        items: list[dict] = []
        for folder in sorted(p for p in base.iterdir() if p.is_dir()):
            if folder.name in reserved or folder.resolve() in skip:
                continue
            prev = cached.get(folder.name)
            # Sólo se vuelve a abrir el epub si la carpeta cambió. Una entrada
            # sin work_id viene de una caché anterior al layout canonico y hay
            # que rehacerla, o no se podria deduplicar contra la biblioteca.
            if (prev and "work_id" in prev
                    and prev.get("mtime") == int(folder.stat().st_mtime)):
                items.append(prev)
                continue
            entry = self._scan_folder(folder)
            if entry:
                items.append(entry)

        try:
            index_path.write_text(
                json.dumps({"items": items}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass  # sin caché sigue funcionando, sólo más lento

        return items

    # --- filtrado y facets ------------------------------------------------

    @staticmethod
    def _matches(item: dict, filters: dict) -> bool:
        text = (filters.get("q") or "").strip().lower()
        if text:
            haystack = " ".join([
                item.get("title") or "",
                " ".join(item.get("authors") or []),
                " ".join(item.get("publishers") or []),
                item.get("folder") or "",
            ]).lower()
            if text not in haystack:
                return False

        for key, values in (filters.get("facets") or {}).items():
            if not values:
                continue
            if key in ("authors", "publishers", "formats"):
                have = item.get(key) or []
                # "(sin dato)" es un valor seleccionable, no un hueco
                if not have:
                    if "__none__" not in values:
                        return False
                elif not set(have) & set(values):
                    return False
            else:
                have = item.get(key)
                if have in (None, ""):
                    if "__none__" not in values:
                        return False
                elif str(have) not in values:
                    return False

        return True

    @staticmethod
    def _facet_counts(items: list[dict]) -> dict:
        """Conteos por valor. Los vacíos van a __none__ en vez de perderse."""
        groups = {
            "location": {},
            "content_type": {},
            "language": {},
            "year": {},
            "publishers": {},
            "authors": {},
            "formats": {},
        }
        for it in items:
            for key in groups:
                value = it.get(key)
                if key in ("publishers", "authors", "formats"):
                    values = value or ["__none__"]
                else:
                    values = [str(value)] if value not in (None, "") else ["__none__"]
                for v in values:
                    groups[key][v] = groups[key].get(v, 0) + 1

        # Ordenado por frecuencia, y los años de más nuevo a más viejo
        out = {}
        for key, counts in groups.items():
            pairs = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            if key == "year":
                pairs = sorted(counts.items(), key=lambda kv: kv[0], reverse=True)
            out[key] = [{"value": k, "count": v} for k, v in pairs]
        return out

    # Criterios de orden que acepta el visor. El valor por defecto es el
    # alfabético, que es el único estable si falta metadata.
    SORT_OPTIONS = ("title", "title_desc", "year", "year_asc", "size", "added")

    @classmethod
    def _sort_items(cls, items: list[dict], sort: str) -> list[dict]:
        """Ordena el listado. Los valores faltantes van siempre al final."""
        def by_title(it):
            return (it.get("title") or "").lower()

        def by_year(it, newest=True):
            year = it.get("year")
            # Sin año no se puede comparar: se manda al final en ambos sentidos
            if not year:
                return (1, 0)
            return (0, -int(year) if newest else int(year))

        if sort == "title_desc":
            return sorted(items, key=by_title, reverse=True)
        if sort == "year":
            return sorted(items, key=lambda it: (by_year(it, True), by_title(it)))
        if sort == "year_asc":
            return sorted(items, key=lambda it: (by_year(it, False), by_title(it)))
        if sort == "size":
            return sorted(items, key=lambda it: -(it.get("size_bytes") or 0))
        if sort == "added":
            return sorted(items, key=lambda it: -(it.get("mtime") or 0))
        return sorted(items, key=by_title)

    def browse(
        self,
        query: str = "",
        facets: dict | None = None,
        refresh: bool = False,
        output_dir: Path | None = None,
        sort: str = "title",
    ) -> dict:
        """Índice filtrado y ordenado + conteos de facets sobre TODA la biblioteca."""
        items = self.scan(output_dir=output_dir, refresh=refresh)
        filters = {"q": query, "facets": facets or {}}
        matched = [it for it in items if self._matches(it, filters)]

        if sort not in self.SORT_OPTIONS:
            sort = "title"
        matched = self._sort_items(matched, sort)

        return {
            "items": matched,
            "total": len(items),
            "shown": len(matched),
            # Los conteos son del total, no de lo filtrado: así el usuario ve
            # cuánto hay disponible en cada valor antes de elegirlo.
            "facets": self._facet_counts(items),
            "sort": sort,
            "total_bytes": sum(it.get("size_bytes") or 0 for it in items),
        }

"""Transferir EPUB a Calibre-Web Automated.

CWA ingesta por carpeta vigilada: se deja un EPUB en su carpeta de ingesta y el
lo importa por su cuenta. Dos avisos de su documentacion mandan sobre el diseno
de esto:

1. Todo lo que hay en la carpeta de ingesta se BORRA despues de procesarse. Es
   una cola, no un almacen. De ahi que aqui solo se COPIE -- los originales de
   la biblioteca y de los bundles no se mueven ni se tocan. Y de ahi tambien que
   la ruta configurada NUNCA deba apuntar a nuestra propia biblioteca.
2. Pide no dejar archivos a medio escribir ahi, porque puede recoger uno
   incompleto. Se copia a `.part` y se renombra al terminar, que es el mismo
   truco que ya usa el downloader con los capitulos.

Y una trampa nuestra: el almacen canoniza los nombres, asi que TODOS los libros
de la biblioteca se llaman `book.epub`. Copiarlos tal cual a una sola carpeta
seria pisarlos entre si, asi que aqui se renombran a partir del titulo y el
autor.

La deteccion de "CWA activo" es una prueba de escritura real, no un ping: lo que
hace falta saber no es si la Pi responde, es si podemos dejar un archivo.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import config
from utils.files import sanitize_filename

from .base import Plugin

SETTING_KEY = "cwa_ingest_dir"
SENT_FILE = "cwa_sent.json"

# Cuanto se lee de cada extremo del archivo para el descarte rapido. No se usa
# como huella: solo para no hashear entero lo que ya se sabe distinto.
_PROBE_BYTES = 65536


class CwaPlugin(Plugin):
    """Inventario deduplicado de EPUB y su copia a la carpeta de CWA."""

    def __init__(self):
        self._lock = threading.RLock()
        self._progress = self._idle()
        self._digests: dict[tuple, str] = {}

    # --- configuracion ----------------------------------------------------

    @staticmethod
    def ingest_dir() -> "Path | None":
        raw = (config.SETTINGS.get(SETTING_KEY) or "").strip()
        return Path(raw) if raw else None

    def status(self) -> dict:
        """Esta CWA disponible, y si no, por que exactamente.

        Se distingue "no configurado" de "configurado y no alcanzable" porque la
        UI hace cosas distintas: en el primer caso pide la ruta, en el segundo
        dice que arreglar.
        """
        directory = self.ingest_dir()
        if directory is None:
            return {"configured": False, "ok": False, "path": "",
                    "reason": "no hay carpeta de ingesta configurada"}

        ok, reason = self._writable(directory)
        return {"configured": True, "ok": ok, "path": str(directory),
                "reason": reason}

    def set_path(self, raw: str) -> dict:
        """Guarda la ruta si de verdad se puede escribir en ella.

        Se valida ANTES de guardar: dejar guardada una ruta que no sirve solo
        traslada el fallo al momento de transferir, cuando ya has seleccionado
        setenta libros.

        Y se traduce antes de validar, porque la ruta natural de escribir es la
        de la Pi y Windows no la entiende.
        """
        if not (raw or "").strip():
            return {"ok": False, "path": "", "reason": "la ruta esta vacia"}

        resuelta = self.resolve_path(raw)
        candidate = resuelta["path"]
        directory = Path(candidate)
        ok, reason = self._writable(directory)

        if not ok:
            # Si venia en formato POSIX se dice lo que se intento, que es la
            # unica pista util: "no existe" a secas manda a nadie a ninguna
            # parte.
            if candidate.startswith("/"):
                if resuelta["tried"]:
                    reason = (
                        "esa es una ruta de la Pi. Se probo por red "
                        + ", ".join(resuelta["tried"][:3])
                        + " y no se pudo escribir: revisa que el recurso "
                          "compartido permita escritura"
                    )
                else:
                    reason = (
                        "esa es una ruta de la Pi y desde Windows no existe. "
                        "Hace falta la ruta de red del recurso compartido, "
                        "tipo \\\\192.168.100.2\\recurso\\ingest"
                    )
            return {"ok": False, "path": candidate, "reason": reason,
                    "tried": resuelta["tried"]}

        config.save_setting(SETTING_KEY, str(directory))
        return {"ok": True, "path": str(directory), "reason": "",
                "translated": resuelta["translated"]}

    def _writable(self, directory: Path) -> tuple[bool, str]:
        """Prueba de escritura de verdad. Un archivo temporal que se borra.

        `is_dir()` no basta: un recurso de red puede existir y estar montado en
        solo lectura, y el permiso solo se sabe intentandolo.
        """
        # Guarda contra el peor accidente posible: CWA borra lo que hay en su
        # carpeta de ingesta, asi que apuntarla a nuestra biblioteca seria
        # autorizarle a vaciarla.
        try:
            propia = Path(config.LIBRARY_DIR).resolve()
            if directory.resolve() == propia or propia in directory.resolve().parents:
                return False, ("esa ruta esta dentro de tu biblioteca, y CWA "
                               "borra lo que hay en su carpeta de ingesta")
        except OSError:
            pass

        if not directory.exists():
            return False, "la carpeta no existe o no se alcanza desde este equipo"
        if not directory.is_dir():
            return False, "esa ruta no es una carpeta"

        probe = directory / f".oreilly-ingest-{os.getpid()}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return False, f"no se puede escribir: {exc.strerror or exc}"
        return True, ""

    # --- traducir rutas de la Pi ------------------------------------------

    @staticmethod
    def _net(args: list) -> str:
        """Salida de un comando `net`, o cadena vacia si no se puede.

        subprocess y no una libreria de SMB: Windows ya trae `net`, y anadir una
        dependencia para leer una lista de recursos compartidos no se sostiene.
        """
        try:
            done = subprocess.run(
                ["net"] + args,
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001 - sin `net` no se afirma nada
            return ""
        return (done.stdout or b"").decode("utf-8", "replace") + \
               (done.stderr or b"").decode("utf-8", "replace")

    def _known_hosts(self) -> list:
        """Servidores que este Windows ya conoce, de sus unidades mapeadas."""
        hosts = []
        for match in re.finditer(r"\\\\([^\\\s]+)\\", self._net(["use"])):
            host = match.group(1)
            if host and host not in hosts:
                hosts.append(host)
        return hosts

    def _shares(self, host: str) -> list:
        """Nombres de los recursos que publica `host`."""
        salida = self._net(["view", f"\\\\{host}"])
        nombres = []
        for linea in salida.splitlines():
            # Las filas de la tabla empiezan con el nombre y siguen con el tipo.
            match = re.match(r"^(\S+)\s+Disk\b", linea.strip())
            if match:
                nombres.append(match.group(1))
        return nombres

    def resolve_path(self, raw: str) -> dict:
        """Ruta usable en Windows a partir de lo que haya escrito el usuario.

        Una ruta de Windows se devuelve tal cual. Una ruta POSIX se traduce
        buscando un recurso compartido cuyo nombre coincida con un tramo: el
        recurso `hannah` de la Pi es su [homes], asi que
        /home/hannah/services/cwa/ingest sale como
        \\\\host\\hannah\\services\\cwa\\ingest.
        """
        texto = (raw or "").strip().strip('"')
        if not texto or not texto.startswith("/"):
            return {"path": texto, "translated": False, "tried": []}

        tramos = [t for t in texto.split("/") if t]
        intentos = []
        for host in self._known_hosts():
            for share in self._shares(host):
                for i, tramo in enumerate(tramos):
                    if tramo.lower() != share.lower():
                        continue
                    resto = tramos[i + 1:]
                    candidato = "\\\\" + host + "\\" + share
                    if resto:
                        candidato += "\\" + "\\".join(resto)
                    intentos.append(candidato)
                    ok, _ = self._writable(Path(candidato))
                    if ok:
                        return {"path": candidato, "translated": True,
                                "tried": intentos}

        return {"path": texto, "translated": False, "tried": intentos}

    # --- inventario -------------------------------------------------------

    def inventory(self) -> dict:
        """Todos los EPUB de biblioteca y bundles, sin repetidos.

        El duplicado tipico es byte a byte: el bundle guarda una COPIA del mismo
        archivo que fue a la biblioteca. Asi que se agrupa por contenido, no por
        titulo -- dos ediciones distintas del mismo libro son dos entradas, y el
        mismo archivo en dos sitios es una.
        """
        entradas = list(self._from_library()) + list(self._from_bundles())

        # Agrupado en dos pasos, por coste: primero por tamano, que es gratis, y
        # solo se hashea entero dentro de los grupos que comparten tamano. Con
        # tamanos unicos -- lo normal -- no se hashea nada.
        por_tamano: dict[int, list] = {}
        for entrada in entradas:
            por_tamano.setdefault(entrada["size"], []).append(entrada)

        grupos: dict[str, list] = {}
        for size, lote in por_tamano.items():
            if len(lote) == 1:
                grupos[f"s{size}"] = lote
                continue
            for entrada in lote:
                clave = self._digest(Path(entrada["path"]), size)
                grupos.setdefault(clave, []).append(entrada)

        enviados = self._sent()
        items = []
        for clave, lote in grupos.items():
            # La de la biblioteca manda como copia canonica: es el almacen, y el
            # bundle es una copia suya.
            lote.sort(key=lambda e: 0 if e["source"] == "library" else 1)
            principal = lote[0]
            items.append({
                "key": clave,
                "title": principal["title"],
                "authors": principal["authors"],
                "language": principal["language"],
                "size": principal["size"],
                "source": principal["source"],
                "path": principal["path"],
                "duplicates": [e["path"] for e in lote[1:]],
                "sent": clave in enviados,
                "sent_at": enviados.get(clave, {}).get("at"),
            })

        items.sort(key=lambda e: (e["title"] or "").lower())
        self._assign_names(items)

        return {
            "items": items,
            "total_files": len(entradas),
            "unique": len(items),
            "duplicates": len(entradas) - len(items),
            "already_sent": sum(1 for e in items if e["sent"]),
        }

    def _from_library(self):
        root = Path(config.LIBRARY_DIR) / "objects"
        if not root.is_dir():
            return
        for shard in sorted(root.iterdir()):
            if not shard.is_dir():
                continue
            for obj in sorted(shard.iterdir()):
                if not obj.is_dir():
                    continue
                meta = self._read_json(obj / "metadata.json")
                for epub in sorted(obj.glob("*.epub")):
                    yield {
                        "path": str(epub),
                        "size": epub.stat().st_size,
                        "source": "library",
                        "title": meta.get("title") or obj.name,
                        "authors": meta.get("authors") or [],
                        "language": (meta.get("language") or "").split("-")[0].lower(),
                    }

    def _from_bundles(self):
        root = Path(config.OUTPUT_DIR) / "bundles"
        if not root.is_dir():
            return
        for bundle in sorted(root.iterdir()):
            if not bundle.is_dir():
                continue
            manifest = self._read_json(bundle / "bundle.json")
            idiomas = manifest.get("languages") or {}
            for lang_dir in sorted(bundle.iterdir()):
                if not lang_dir.is_dir():
                    continue
                entrada = idiomas.get(lang_dir.name) or {}
                for epub in sorted(lang_dir.glob("*.epub")):
                    yield {
                        "path": str(epub),
                        "size": epub.stat().st_size,
                        "source": "bundle",
                        "title": entrada.get("title") or manifest.get("title") or bundle.name,
                        "authors": entrada.get("authors") or [],
                        "language": lang_dir.name.lower(),
                    }

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _digest(self, path: Path, size: int) -> str:
        """sha256 del archivo, memorizado por (ruta, tamano, fecha).

        Solo se llama cuando dos archivos comparten tamano, que es cuando hace
        falta saber de verdad si son el mismo.
        """
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return f"nostat:{path}"

        clave = (str(path), size, mtime)
        if clave in self._digests:
            return self._digests[clave]

        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for bloque in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(bloque)
        except OSError:
            # Sin poder leerlo no se puede afirmar que sea igual a nada: se le
            # da una clave propia para que no se agrupe con otro por error.
            return f"unread:{path}"

        digest = h.hexdigest()
        self._digests[clave] = digest
        return digest

    @staticmethod
    def _assign_names(items: list) -> None:
        """Nombre de archivo definitivo para cada entrada, sin colisiones.

        Hace falta porque en la biblioteca TODOS los epub se llaman `book.epub`:
        copiarlos con su nombre a una sola carpeta seria perder todos menos uno.
        """
        usados: set[str] = set()
        for item in items:
            titulo = (item["title"] or "libro").strip()
            autor = (item["authors"] or [""])[0]
            base = f"{titulo} - {autor}".strip(" -") if autor else titulo
            nombre = sanitize_filename(base) or "libro"

            # El idioma desempata primero, porque es informativo. Un numero
            # despues, que no dice nada pero garantiza unicidad.
            candidato = nombre
            if candidato.lower() in usados and item["language"]:
                candidato = f"{nombre} ({item['language']})"
            n = 2
            while candidato.lower() in usados:
                candidato = f"{nombre} ({n})"
                n += 1

            usados.add(candidato.lower())
            item["filename"] = f"{candidato}.epub"

    # --- registro de lo enviado -------------------------------------------

    @property
    def _sent_store(self) -> Path:
        base = config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR
        return base / SENT_FILE

    def _sent(self) -> dict:
        """Lo ya enviado, por clave de contenido.

        Registro propio y no consulta a CWA: su carpeta de ingesta se vacia
        sola, asi que mirarla no dice nada. Esto no sabe si borraste el libro en
        CWA -- es un recordatorio de lo que mandamos, no un espejo de su
        biblioteca.
        """
        data = self._read_json(self._sent_store)
        return data if isinstance(data, dict) else {}

    def _remember(self, clave: str, filename: str) -> None:
        with self._lock:
            data = self._sent()
            data[clave] = {"filename": filename, "at": time.time()}
            try:
                self._sent_store.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._sent_store.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(self._sent_store)
            except OSError:
                pass  # sin registro se sigue transfiriendo, solo no se recuerda

    # --- transferencia ----------------------------------------------------

    @staticmethod
    def _idle() -> dict:
        return {"running": False, "done": 0, "total": 0, "current": "",
                "ok": [], "failed": [], "finished": False, "error": ""}

    def progress(self) -> dict:
        with self._lock:
            return dict(self._progress)

    def transfer(self, keys: list) -> dict:
        """Arranca la copia en segundo plano. Devuelve el estado inicial."""
        with self._lock:
            if self._progress.get("running"):
                return {"error": "ya hay una transferencia en marcha"}

        estado = self.status()
        if not estado["ok"]:
            return {"error": estado["reason"] or "CWA no esta disponible"}

        inventario = self.inventory()
        por_clave = {i["key"]: i for i in inventario["items"]}
        seleccion = [por_clave[k] for k in (keys or []) if k in por_clave]
        if not seleccion:
            return {"error": "no hay nada seleccionado"}

        with self._lock:
            self._progress = self._idle()
            self._progress.update({"running": True, "total": len(seleccion)})

        hilo = threading.Thread(
            target=self._run, args=(seleccion, Path(estado["path"])), daemon=True)
        hilo.start()
        return {"started": True, "total": len(seleccion)}

    def _run(self, seleccion: list, destino: Path) -> None:
        for item in seleccion:
            with self._lock:
                self._progress["current"] = item["filename"]

            try:
                self._copy_one(Path(item["path"]), destino, item["filename"])
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._progress["failed"].append(
                        {"title": item["title"], "reason": str(exc)})
            else:
                self._remember(item["key"], item["filename"])
                with self._lock:
                    self._progress["ok"].append(item["filename"])

            with self._lock:
                self._progress["done"] += 1

        with self._lock:
            self._progress["running"] = False
            self._progress["finished"] = True
            self._progress["current"] = ""

    @staticmethod
    def _copy_one(origen: Path, destino: Path, filename: str) -> None:
        """Copia un EPUB dejandolo visible solo cuando esta completo.

        A `.part` y luego renombrar: CWA vigila la carpeta y avisa en su
        documentacion de que puede recoger un archivo a medio escribir.
        """
        final = destino / filename
        parcial = destino / (filename + ".part")
        try:
            shutil.copy2(origen, parcial)
            parcial.replace(final)
        except Exception:
            # Un `.part` abandonado confundiria una revision posterior de la
            # carpeta, y CWA no lo va a limpiar porque no es un libro.
            try:
                parcial.unlink(missing_ok=True)
            except OSError:
                pass
            raise

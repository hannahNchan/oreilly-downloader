"""Lista de "para despues": libros que quieres bajar, pero todavia no.

Es la otra punta del ciclo respecto a los favoritos que ya existian: aquellos
son una estrella sobre obras que YA tienes en disco, para fijarlas arriba en la
biblioteca. Esto es lo contrario — titulos que aun no has descargado.

Se guarda en JSON junto a los demas ajustes del usuario (data/, ya ignorado por
git), asi que sobrevive a los reinicios y no viaja en el repositorio.

Lo que NO se guarda aqui es si el libro ya se descargo: eso se pregunta a la
biblioteca cada vez. Guardarlo significaria que se quedaria desactualizado en
cuanto borraras el archivo del disco.
"""

import json
import time
from pathlib import Path

import config

from .base import Plugin

WATCHLIST_FILE = "watchlist.json"

# Lo unico que se guarda de cada titulo: lo justo para pintar la tarjeta sin
# tener que volver a pedirselo a O'Reilly (que exigiria sesion valida).
FIELDS = ("book_id", "title", "authors", "publishers", "year",
          "content_type", "cover_url")


class WatchlistPlugin(Plugin):
    """Lista de titulos guardados para descargar mas tarde."""

    @property
    def _store(self) -> Path:
        base = config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR
        return base / WATCHLIST_FILE

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, items: list[dict]) -> None:
        """Escritura a temporal y rename: un corte no deja la lista a medias."""
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._store)
        except OSError:
            pass  # sin persistencia la sesion sigue, solo no sobrevive al cierre

    # --- API --------------------------------------------------------------

    def items(self) -> list[dict]:
        """Lo guardado, lo mas reciente primero."""
        return sorted(self._read(), key=lambda e: e.get("added_at") or 0,
                      reverse=True)

    def has(self, book_id: str) -> bool:
        return any(e.get("book_id") == str(book_id) for e in self._read())

    def add(self, book: dict) -> dict:
        """Guarda un titulo. Repetirlo no lo duplica ni lo reordena."""
        book_id = str(book.get("book_id") or book.get("id") or "").strip()
        if not book_id:
            raise ValueError("hace falta el book_id")

        items = self._read()
        existing = next((e for e in items if e.get("book_id") == book_id), None)
        if existing:
            return existing

        entry = {k: book.get(k) for k in FIELDS}
        entry["book_id"] = book_id
        entry["added_at"] = time.time()
        items.append(entry)
        self._write(items)
        return entry

    def remove(self, book_id: str) -> bool:
        book_id = str(book_id)
        items = self._read()
        left = [e for e in items if e.get("book_id") != book_id]
        if len(left) == len(items):
            return False
        self._write(left)
        return True

    def clear_downloaded(self) -> int:
        """Quita de la lista todo lo que ya esta en la biblioteca.

        El "ya descargado" se decide con annotated(), o sea preguntando al
        disco: si borras el archivo, el titulo se queda en la lista, que es lo
        que quieres. Se escribe una sola vez, no una por titulo.
        """
        hechos = {e["book_id"] for e in self.annotated() if e.get("downloaded")}
        if not hechos:
            return 0
        items = self._read()
        left = [e for e in items if e.get("book_id") not in hechos]
        self._write(left)
        return len(items) - len(left)

    def ids(self) -> set[str]:
        """Solo los ids: lo que necesita la busqueda para pintar el cintillo."""
        return {e.get("book_id") for e in self._read() if e.get("book_id")}

    # --- estado derivado --------------------------------------------------

    def annotated(self) -> list[dict]:
        """La lista con el estado de descarga resuelto contra la biblioteca.

        `downloaded` no vive en el JSON a proposito: se calcula mirando lo que
        hay de verdad en disco, asi que no puede quedarse desactualizado.
        """
        library = self.kernel["library"]
        try:
            en_disco = {
                str(i.get("book_id")): i
                for i in library.scan() if i.get("book_id")
            }
        except Exception:  # noqa: BLE001 - una biblioteca ilegible no rompe la lista
            en_disco = {}

        queue = self.kernel.get("queue")
        activos = {}
        if queue is not None:
            for job in queue.snapshot().get("jobs", []):
                if job.get("status") not in ("completed", "error", "cancelled"):
                    activos[str(job.get("book_id"))] = job

        out = []
        for entry in self.items():
            book_id = entry["book_id"]
            obra = en_disco.get(book_id)
            job = activos.get(book_id)
            out.append({
                **entry,
                "downloaded": obra is not None,
                "library_folder": obra.get("folder") if obra else None,
                "job_status": job.get("status") if job else None,
                "job_percentage": job.get("percentage") if job else None,
            })
        return out

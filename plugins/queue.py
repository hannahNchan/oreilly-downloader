"""Cola de descargas: una a la vez, con pausa cuando caduca la sesion.

Antes solo cabia una descarga: el servidor guardaba un unico diccionario de
progreso y rechazaba la segunda con un 409. Aqui las peticiones se encolan y un
solo hilo las consume en orden.

Es secuencial a proposito, no por simplicidad. El `HttpClient` es compartido y
resetea el jar de cookies antes de cada peticion, asi que dos descargas a la vez
se pisarian; y subir el ritmo contra O'Reilly es exactamente lo que dispara los
bloqueos que ya nos han costado descargas.

Si la sesion caduca a mitad, el trabajo NO se pierde: pasa a `paused`, la cola
entera se detiene y al pegar cookies nuevas se reintenta. Reintentar es barato
porque los capitulos ya bajados estan en cache y las pistas de audio existentes
se saltan.
"""

import json
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import config

from .base import Plugin
from .errors import SessionExpired

QUEUE_FILE = "queue.json"

# Estados finales: un trabajo aqui ya no vuelve a moverse
DONE_STATES = ("completed", "error", "cancelled")


@dataclass
class Job:
    """Una descarga encolada."""

    id: str
    book_id: str
    title: str
    content_type: str = "book"
    formats: list = field(default_factory=lambda: ["epub"])
    target_lang: str | None = None
    skip_images: bool = False
    transfer: bool = True
    chapters: list | None = None
    # Como dict y no como ChunkConfig: el trabajo se persiste en JSON, y una
    # dataclass no sobrevive a eso.
    chunking: dict | None = None

    status: str = "queued"       # queued|running|paused|completed|error|cancelled
    phase: str = ""              # el `status` que reporta el downloader
    percentage: int = 0
    message: str = ""
    current_chapter: int = 0
    total_chapters: int = 0
    error: str | None = None
    files: dict = field(default_factory=dict)
    format_errors: dict = field(default_factory=dict)
    created_at: float = 0.0

    def public(self, position: int | None = None) -> dict:
        data = asdict(self)
        data["position"] = position
        return data


class QueuePlugin(Plugin):
    """Cola secuencial de descargas."""

    def __init__(self):
        self._jobs: list[Job] = []
        self._lock = threading.RLock()
        self._wakeup = threading.Event()     # hay trabajo nuevo que mirar
        self._session_ok = threading.Event()  # cookies frescas disponibles
        self._session_ok.set()
        self._cancelled: set[str] = set()
        self._worker: threading.Thread | None = None
        self._owner = False   # lo pone claim()
        self._lock_handle = None  # abierto mientras seamos el dueno

    # --- persistencia -----------------------------------------------------

    @property
    def _store(self) -> Path:
        base = config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR
        return base / QUEUE_FILE

    def _save(self) -> None:
        """Guarda lo que sigue vivo.

        Una cola pausada esperando cookies puede durar horas, asi que perderla
        por reiniciar el servidor seria absurdo. El progreso no se guarda: al
        reanudar se recalcula, y los capitulos ya estan en cache.
        """
        with self._lock:
            pending = [asdict(j) for j in self._jobs if j.status not in DONE_STATES]
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps(pending, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._store)
        except OSError:
            pass  # sin persistencia se sigue trabajando, solo no sobrevive al reinicio

    # --- exclusion entre servidores ---------------------------------------

    @property
    def _lock_file(self) -> Path:
        return self._store.with_suffix(".lock")

    @staticmethod
    def _lock_bytes(fh) -> None:
        """Cierra un cerrojo exclusivo sobre el archivo. Lanza si ya lo tiene otro.

        Lo sostiene el sistema operativo, no nosotros: cuando el proceso muere
        —aunque sea a lo bruto— el cerrojo se suelta solo. La version anterior
        guardaba un PID en el archivo y preguntaba si ese proceso vivia, y eso
        falla de dos maneras: los PID se reciclan, y durante los dos segundos
        que tarda en morir el servidor anterior el nuevo lo ve vivo.
        """
        fh.seek(0)
        try:
            import msvcrt
        except ImportError:
            pass
        else:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        try:
            import fcntl
        except ImportError:
            return   # sin primitiva de bloqueo: mas vale trabajar que no arrancar
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def claim(self) -> bool:
        """Intenta quedarse con la cola. False si ya la tiene otro servidor.

        La cola vive en un archivo compartido, asi que DOS servidores apuntando
        al mismo data/ se pondrian a descargar lo mismo a la vez: escribirian en
        las mismas carpetas y doblarian el ritmo de peticiones contra O'Reilly,
        que es justo lo que dispara los bloqueos. El segundo se queda mirando:
        sirve la cola por API pero no ejecuta nada.

        Se puede llamar tantas veces como haga falta: el trabajador la reintenta
        mientras no sea el dueno, para que al cerrarse el otro servidor este tome
        el relevo sin reiniciar nada.
        """
        import os
        with self._lock:
            if self._owner:
                return True
            try:
                self._lock_file.parent.mkdir(parents=True, exist_ok=True)
                fh = open(self._lock_file, "a+", encoding="utf-8")
            except OSError:
                return False
            try:
                self._lock_bytes(fh)
            except OSError:
                fh.close()          # lo tiene otro proceso, y sigue vivo
                return False

            # El handle se guarda abierto a proposito: es lo que sostiene el
            # cerrojo mientras el proceso viva.
            self._lock_handle = fh
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()))
                fh.flush()          # solo informativo, para saber quien la tiene
            except OSError:
                pass
            self._owner = True
            return True

    def load(self) -> int:
        """Recupera la cola de disco. Se llama al arrancar el servidor."""
        try:
            saved = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0

        restored = 0
        with self._lock:
            for raw in saved:
                try:
                    job = Job(**raw)
                except TypeError:
                    continue  # formato viejo: se ignora en vez de reventar
                # Lo que estaba corriendo cuando se apago vuelve a la cola: no
                # hay forma de saber por donde iba, y reintentar es barato.
                #
                # Lo PAUSADO tambien vuelve a la cola, y no se queda pausado:
                # tras un reinicio no sabemos si las cookies siguen caducadas, y
                # dejarlo en pausa lo condenaba a no arrancar nunca mientras el
                # resto de la cola seguia y fallaba uno a uno. Si la sesion
                # sigue mal, se vuelve a pausar sola en el primer intento.
                if job.status in ("running", "paused"):
                    job.status = "queued"
                    job.percentage = 0
                    job.message = ""
                self._jobs.append(job)
                restored += 1
        if restored:
            self._wakeup.set()
            self._ensure_worker()
        return restored

    # --- API de la cola ---------------------------------------------------

    def enqueue(self, **kwargs) -> Job:
        """Anade un trabajo. Si no hay nada corriendo, arranca solo."""
        book_id = kwargs.get("book_id")
        with self._lock:
            # Duplicados: dos trabajos del mismo libro escribirian en la misma
            # carpeta y se pisarian.
            for job in self._jobs:
                if job.book_id == book_id and job.status not in DONE_STATES:
                    return job

            job = Job(id=uuid.uuid4().hex[:12], created_at=time.time(), **kwargs)
            self._jobs.append(job)

        self._save()
        self._wakeup.set()
        self._ensure_worker()
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None or job.status in DONE_STATES:
                return False
            self._cancelled.add(job_id)
            if job.status in ("queued", "paused"):
                job.status = "cancelled"
        self._save()
        self._wakeup.set()
        self._session_ok.set()   # despierta al trabajador si estaba en pausa
        return True

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.status not in DONE_STATES]
            return before - len(self._jobs)

    def session_refreshed(self) -> int:
        """Cookies nuevas: se reanuda TODO lo pausado. Devuelve cuantos.

        Todos los pausados lo estan por la misma causa, y acaba de resolverse.
        """
        resumed = 0
        with self._lock:
            for job in self._jobs:
                if job.status == "paused":
                    job.status = "queued"
                    job.message = ""
                    resumed += 1
            # Dentro del lock, por lo mismo: asi no puede intercalarse con el
            # clear() del trabajador al pausarse.
            self._session_ok.set()
        self._wakeup.set()
        self._save()
        return resumed

    def snapshot(self) -> dict:
        """Estado completo para la UI."""
        with self._lock:
            pending = [j for j in self._jobs if j.status in ("queued", "paused")]
            order = {j.id: i + 1 for i, j in enumerate(pending)}
            jobs = [j.public(order.get(j.id)) for j in self._jobs]
            running = next((j for j in self._jobs if j.status == "running"), None)
            paused = [j for j in self._jobs if j.status == "paused"]
            return {
                "jobs": jobs,
                "active": sum(1 for j in self._jobs if j.status not in DONE_STATES),
                "running_id": running.id if running else None,
                # La cola entera se detiene con uno pausado: el siguiente
                # chocaria con la misma pared de autenticacion.
                "paused": bool(paused),
                # Si no somos el dueno, aqui no se ejecuta nada. Se dice, en vez
                # de dejar la lista en "En cola" sin explicacion.
                "owner": self._owner,
            }

    def running_job(self) -> dict | None:
        with self._lock:
            job = next((j for j in self._jobs if j.status == "running"), None)
            if job is None:
                # Para que la UI vea el resultado del ultimo, no un hueco
                finished = [j for j in self._jobs if j.status in DONE_STATES]
                job = finished[-1] if finished else None
            return job.public() if job else None

    # --- trabajador -------------------------------------------------------

    # Cada cuanto se vuelve a intentar tomar la cola cuando la tiene otro
    _CLAIM_RETRY = 5

    def _ensure_worker(self) -> None:
        # El hilo arranca aunque la cola sea de otro: el propio hilo reintenta
        # tomarla. Antes se devolvia aqui sin mas, y entonces la unica manera de
        # recuperarse era reiniciar el servidor.
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def _next_job(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.status == "queued"), None)

    def _run(self) -> None:
        while True:
            if not self._owner and not self.claim():
                # La cola es de otro servidor. No se abandona: se reintenta,
                # porque ese otro puede cerrarse en cualquier momento. Decidirlo
                # una sola vez al arrancar era el fallo: un solape de dos
                # segundos con el servidor anterior dejaba a este de espectador
                # para siempre — la cola se veia crecer y nada se descargaba.
                time.sleep(self._CLAIM_RETRY)
                continue

            job = self._next_job()
            if job is None:
                self._wakeup.clear()
                # Espera con timeout en vez de bloquear para siempre: asi el
                # hilo puede morir si no hay nada que hacer en mucho rato.
                if not self._wakeup.wait(timeout=300):
                    return
                continue

            if job.id in self._cancelled:
                with self._lock:
                    job.status = "cancelled"
                self._save()
                continue

            self._execute(job)

    def _execute(self, job: Job) -> None:
        with self._lock:
            job.status = "running"
            job.error = None
        self._save()

        def report(**kw):
            with self._lock:
                job.phase = kw.get("status") or job.phase
                if kw.get("percentage") is not None:
                    job.percentage = int(kw["percentage"])
                job.message = kw.get("message") or ""
                job.current_chapter = kw.get("current_chapter") or job.current_chapter
                job.total_chapters = kw.get("total_chapters") or job.total_chapters

        def cancel_check():
            return job.id in self._cancelled

        try:
            if job.content_type == "audiobook":
                self._run_audiobook(job, report, cancel_check)
            else:
                self._run_book(job, report, cancel_check)

        except SessionExpired as exc:
            # Lo unico que no se da por perdido: se pausa y se espera.
            #
            # `clear()` va DENTRO del lock, junto al cambio de estado. Estando
            # fuera, session_refreshed() podia colarse entre ambos: ponia el
            # trabajo en cola y hacia set(), y justo despues este clear()
            # borraba el aviso. El trabajador se quedaba esperando para siempre
            # un evento que ya nadie iba a mandar, con el trabajo mostrandose
            # como "en cola". Sintoma: pegabas cookies nuevas y no pasaba nada.
            with self._lock:
                job.status = "paused"
                job.message = str(exc)
                self._session_ok.clear()
            self._save()

            # Y se espera en bucle releyendo el estado, no con un wait() eterno:
            # si algun aviso se perdiera, esto se recupera solo en lugar de
            # dejar la cola muerta.
            while not self._session_ok.wait(timeout=2):
                with self._lock:
                    if job.status != "paused" or job.id in self._cancelled:
                        break

            with self._lock:
                if job.status == "paused":
                    job.status = "queued"
            self._save()
            return

        except Exception as exc:  # noqa: BLE001
            cancelled = job.id in self._cancelled or "cancel" in str(exc).lower()
            with self._lock:
                job.status = "cancelled" if cancelled else "error"
                job.error = None if cancelled else f"{type(exc).__name__}: {exc}"
            if not cancelled:
                traceback.print_exc()
            self._save()
            return

        with self._lock:
            job.status = "completed"
            job.percentage = 100
        self._save()

    def _run_book(self, job: Job, report, cancel_check) -> None:
        chunk_config = None
        if job.chunking:
            from .chunking import ChunkConfig
            chunk_config = ChunkConfig(
                chunk_size=job.chunking.get("chunk_size", 4000),
                overlap=job.chunking.get("overlap", 200),
                respect_boundaries=True,
            )

        result = self.kernel["downloader"].download(
            book_id=job.book_id,
            output_dir=self.kernel["output"].get_default_dir(),
            formats=job.formats,
            selected_chapters=job.chapters,
            skip_images=job.skip_images,
            chunk_config=chunk_config,
            target_lang=job.target_lang,
            transfer=job.transfer,
            progress_callback=lambda p: report(
                status=p.status, percentage=p.percentage, message=p.message,
                current_chapter=p.current_chapter, total_chapters=p.total_chapters,
            ),
            cancel_check=cancel_check,
        )
        with self._lock:
            job.title = result.title or job.title
            job.files = {k: str(v) for k, v in (result.files or {}).items()}
            job.format_errors = dict(result.errors or {})

    def _run_audiobook(self, job: Job, report, cancel_check) -> None:
        result = self.kernel["audiobook"].download(
            book_id=job.book_id,
            output_dir=self.kernel["output"].get_default_dir(),
            selected_chapters=job.chapters,
            transfer=job.transfer,
            progress_callback=report,
            cancel_check=cancel_check,
        )
        with self._lock:
            job.title = result.title or job.title
            job.files = {"audiobook": str(result.output_dir)}

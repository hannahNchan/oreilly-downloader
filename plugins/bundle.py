"""Bundles: the same book in two languages, in every format, in one folder.

A bundle is not a new download path. It is two ordinary queue jobs that happen
to share a `bundle_id`, plus a step that files their output away when each one
finishes.

That matters: the queue already knows how to pause when the O'Reilly session
expires, cancel, resume, report progress and hold the single-owner lock. A
bundle-specific downloader would have to grow all of that again and would grow
the same bugs again.

Layout on disk:

    output/bundles/<slug>/
    |-- en/           markdown, json, plaintext, epub, pdf (+ images)
    |-- es/           the same
    `-- bundle.json   which editions, their ids, what failed

There is no central registry. The job carries the slug, the folder is named
after it, and bundle.json describes itself -- so the state survives a restart
and can be read without this process running.
"""

import json
import shutil
import time
from pathlib import Path

import config
from utils.files import slugify

from .base import Plugin

# Exactly the five formats the feature promises, listed in the order the
# downloader actually generates them. `toon` and `chunks` are left out on
# purpose: they are LLM/RAG artefacts, not something you read.
BUNDLE_FORMATS = ["markdown", "json", "plaintext", "pdf", "epub"]

BUNDLES_DIRNAME = "bundles"
MANIFEST_NAME = "bundle.json"


class BundlePlugin(Plugin):
    """Plans bundles and files away what the queue produces."""

    # --- planning ---------------------------------------------------------

    def root(self) -> Path:
        return self.kernel["output"].get_default_dir() / BUNDLES_DIRNAME

    def bundle_dir(self, bundle_id: str) -> Path:
        return self.root() / bundle_id

    def plan(self, source: dict, counterpart: dict) -> dict:
        """Work out the slug and the two job specs for a bundle.

        `source` and `counterpart` are the briefs from the editions plugin.
        Returns {"bundle_id", "dir", "jobs": [{book_id, title, lang}, ...]}.
        """
        title = (source.get("title") or counterpart.get("title") or "").strip()
        bundle_id = slugify(title or str(source.get("id") or "bundle"))

        source_lang = self._short_lang(source.get("language"), "en")
        target_lang = self._short_lang(counterpart.get("language"), "es")

        return {
            "bundle_id": bundle_id,
            "dir": str(self.bundle_dir(bundle_id)),
            "title": title,
            "jobs": [
                {
                    "book_id": str(source.get("id") or ""),
                    "title": source.get("title") or title,
                    "lang": source_lang,
                },
                {
                    "book_id": str(counterpart.get("id") or ""),
                    "title": counterpart.get("title") or title,
                    "lang": target_lang,
                },
            ],
        }

    def start(self, plan: dict, source: dict, counterpart: dict) -> dict:
        """Create the folder and the manifest before anything downloads.

        Written up front so a bundle that dies half way still says on disk what
        it was trying to be.
        """
        directory = Path(plan["dir"])
        directory.mkdir(parents=True, exist_ok=True)

        manifest = self._read(directory)
        manifest.update({
            "bundle_id": plan["bundle_id"],
            "title": plan["title"],
            "created_at": manifest.get("created_at") or time.time(),
            "formats": list(BUNDLE_FORMATS),
        })
        languages = manifest.setdefault("languages", {})
        for spec, brief in zip(plan["jobs"], (source, counterpart)):
            entry = languages.setdefault(spec["lang"], {})
            entry.update({
                "book_id": spec["book_id"],
                "title": spec["title"],
                "authors": brief.get("authors") or [],
                "status": entry.get("status") or "queued",
                "files": entry.get("files") or {},
            })
        manifest["match_score"] = counterpart.get("score")
        self._write(directory, manifest)
        return manifest

    # --- collecting -------------------------------------------------------

    def collect(self, job) -> None:
        """File one finished job's output into its language folder.

        Never raises: a bundle is a convenience on top of a download that has
        already succeeded, and a copy failure must not turn a good download into
        a failed job.
        """
        bundle_id = getattr(job, "bundle_id", None)
        language = getattr(job, "bundle_lang", None)
        if not bundle_id or not language:
            return

        try:
            directory = self.bundle_dir(bundle_id)
            target = directory / language
            target.mkdir(parents=True, exist_ok=True)

            manifest = self._read(directory)
            entry = manifest.setdefault("languages", {}).setdefault(language, {})
            entry["book_id"] = job.book_id
            entry["title"] = job.title
            entry["status"] = job.status
            entry["job_id"] = job.id
            entry["finished_at"] = time.time()
            if job.status != "completed":
                entry["error"] = job.error or job.message or job.status
                self._write(directory, manifest)
                return

            copied, _ = self._copy_outputs(job.files or {}, target)

            # Rescate: lo que la descarga no dejo copiar puede estar de todas
            # formas en la biblioteca, porque la transferencia mueve la carpeta
            # y una ruta rancia en job.files hace que _copy_outputs no encuentre
            # nada. Antes eso se daba por perdido.
            rescued = self._copy_from_library(job.book_id, target)

            # La verdad la manda el disco, no lo que creemos haber copiado. Y
            # se mide DESPUES de copiar, asi que una descarga parcial (solo el
            # PDF que faltaba) suma en vez de borrar los otros cuatro: antes
            # esta entrada se sobrescribia entera y se perdia el registro.
            present = sorted(self.formats_in(target))
            entry["files"] = {fmt: str(target) for fmt in present}
            entry["missing"] = [f for f in BUNDLE_FORMATS if f not in present]
            entry.pop("error", None)
            manifest["complete"] = self._is_complete(manifest)
            self._write(directory, manifest)

            detail = f"{len(present)}/{len(BUNDLE_FORMATS)} en disco"
            if rescued:
                detail += f" (rescatados de la biblioteca: {', '.join(rescued)})"
            print(f"[BUNDLE] {bundle_id}/{language}: {detail}")
        except Exception as exc:
            print(f"[BUNDLE] no se pudo archivar {bundle_id}/{language}: "
                  f"{type(exc).__name__}: {exc}")

    def _copy_from_library(self, book_id: str, target: Path) -> list:
        """Copy whatever is still missing straight out of the library object.

        The download writes into output/<slug>/ and the transfer step then MOVES
        that folder into the library. Any path captured before the move is dead,
        and there is nothing in the error to say so -- the copy just finds
        nothing. Reading the final resting place instead makes this immune to
        that whole class of problem.
        """
        rescued: list = []
        folder = self._library_folder(str(book_id))
        if folder is None:
            return rescued

        have = self.formats_in(target)
        for fmt, source in self.formats_in(folder).items():
            if fmt in have:
                continue
            try:
                self._copy_one_any(source, target)
                rescued.append(fmt)
            except Exception as exc:  # noqa: BLE001
                print(f"[BUNDLE] rescate de '{fmt}' fallo: {type(exc).__name__}: {exc}")
        return rescued

    def _library_folder(self, book_id: str) -> "Path | None":
        """Where this book ended up in the library, read off disk."""
        root = Path(config.OUTPUT_DIR) / "library" / "objects"
        if not root.is_dir():
            return None
        for meta in root.rglob("metadata.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if str(data.get("book_id") or "") == book_id:
                return meta.parent
        return None

    def _copy_one_any(self, source, target: Path):
        if isinstance(source, list):
            return [self._copy_one(Path(item), target) for item in source]
        return self._copy_one(Path(source), target)

    def _copy_outputs(self, files: dict, target: Path) -> tuple[dict, list]:
        """Copy what the downloader produced. Only the five bundle formats.

        `files` values are not uniform: most are a path to a file, `markdown` is
        a path to a directory, and `pdf` can be a list when the book was split
        per chapter. All three shapes turn up in practice.
        """
        copied: dict = {}
        failed: list = []

        for fmt in BUNDLE_FORMATS:
            value = files.get(fmt)
            if not value:
                failed.append(fmt)
                continue
            try:
                if isinstance(value, list):
                    destinations = [self._copy_one(Path(p), target) for p in value]
                    copied[fmt] = [str(d) for d in destinations if d]
                else:
                    destination = self._copy_one(Path(value), target)
                    if destination is None:
                        failed.append(fmt)
                    else:
                        copied[fmt] = str(destination)
            except Exception as exc:
                print(f"[BUNDLE] fallo copiando '{fmt}': {type(exc).__name__}: {exc}")
                failed.append(fmt)

        return copied, failed

    @staticmethod
    def _copy_one(source: Path, target_dir: Path) -> Path | None:
        if not source.exists():
            return None
        destination = target_dir / source.name
        if source.is_dir():
            # dirs_exist_ok so re-running a bundle refreshes instead of failing.
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
        return destination

    @staticmethod
    def _is_complete(manifest: dict) -> bool:
        languages = manifest.get("languages") or {}
        if len(languages) < 2:
            return False
        return all(entry.get("status") == "completed" for entry in languages.values())

    # --- what is already there --------------------------------------------

    @staticmethod
    def formats_in(folder: Path) -> dict:
        """Which bundle formats are present in a folder, and as what.

        Detected by extension rather than by expected filename: the JSON export
        is named after the book title, and the PDF is one file or one per
        chapter. Looking for "book.json" would find nothing.
        """
        found: dict = {}
        if not folder.is_dir():
            return found

        markdown = folder / "Markdown"
        if markdown.is_dir() and any(markdown.iterdir()):
            found["markdown"] = markdown

        pdfs = []
        for item in sorted(folder.iterdir()):
            if not item.is_file() or item.name.startswith("."):
                continue
            if item.name in ("metadata.json", MANIFEST_NAME, "cover.jpg"):
                continue
            suffix = item.suffix.lower()
            if suffix == ".epub":
                found.setdefault("epub", item)
            elif suffix == ".txt":
                found.setdefault("plaintext", item)
            elif suffix == ".json":
                found.setdefault("json", item)
            elif suffix == ".pdf":
                pdfs.append(item)

        if pdfs:
            found["pdf"] = pdfs if len(pdfs) > 1 else pdfs[0]
        return found

    def gap(self, bundle_id: str, languages: list) -> dict:
        """What a bundle still needs, read off disk.

        Off disk and not out of bundle.json on purpose: the manifest is what we
        believe, the folders are what is true. A backfill, a manual copy or a
        half-finished run all leave those two disagreeing, and the one that
        matters when deciding what to download again is the disk.
        """
        directory = self.bundle_dir(bundle_id)
        have, missing = {}, {}
        for language in languages:
            present = sorted(self.formats_in(directory / language))
            have[language] = present
            missing[language] = [f for f in BUNDLE_FORMATS if f not in present]

        total = len(BUNDLE_FORMATS) * len(languages) if languages else 0
        return {
            "exists": directory.is_dir(),
            "dir": str(directory),
            "have": have,
            "missing": missing,
            "have_count": sum(len(v) for v in have.values()),
            "total": total,
            "complete": total > 0 and all(not v for v in missing.values()),
        }

    # --- reading ----------------------------------------------------------

    def status(self, bundle_id: str) -> dict | None:
        directory = self.bundle_dir(bundle_id)
        if not (directory / MANIFEST_NAME).is_file():
            return None
        return self._read(directory)

    def list_all(self) -> list:
        root = self.root()
        if not root.is_dir():
            return []
        out = []
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / MANIFEST_NAME).is_file():
                out.append(self._read(child))
        return out

    # --- manifest io ------------------------------------------------------

    @staticmethod
    def _short_lang(value, default: str) -> str:
        text = (str(value or "")).split("-")[0].strip().lower()
        return text or default

    @staticmethod
    def _read(directory: Path) -> dict:
        path = directory / MANIFEST_NAME
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt manifest must not block the download that is finishing.
            return {}

    @staticmethod
    def _write(directory: Path, manifest: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / MANIFEST_NAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

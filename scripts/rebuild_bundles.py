"""Refill a bundle's en/ and es/ folders from the library.

    python scripts/rebuild_bundles.py            # muestra que haria
    python scripts/rebuild_bundles.py --apply    # lo hace

Why this exists: the downloader used to leave `result.files` pointing at
`output/<slug>/` even after the transfer step had MOVED that folder into the
library. The bundle step then copied nothing and marked all five formats as
missing, so the download was fine and the bundle folder was empty.

That is fixed at the source now, but the bundles already on disk stay broken
until something refills them. This is that something. It reads each
bundle.json, finds each edition in the library by its book_id, and copies the
five formats across.

Reconciles a bundle folder against the library: copies over whatever the
bundle is missing and rewrites bundle.json from what is actually on disk.

It never downloads and never deletes: worst case it does nothing and says so.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from core.kernel import create_default_kernel  # noqa: E402
from plugins.bundle import BUNDLE_FORMATS, MANIFEST_NAME  # noqa: E402

def process(manifest_path: Path, bundle, apply: bool) -> bool:
    """Refill one bundle from the library.

    Delegates to the bundle plugin so there is ONE implementation of "what is
    in this folder" and "copy the rest from the library". This script used to
    carry its own copy of both, which is how the manifest ended up written two
    different ways depending on who touched it last.
    """
    directory = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print()
    print(f"{manifest.get('bundle_id') or directory.name}")
    print(f"  {manifest.get('title', '')}")

    changed = False
    for language, entry in (manifest.get("languages") or {}).items():
        book_id = str(entry.get("book_id") or "")
        target = directory / language
        before = sorted(bundle.formats_in(target))

        folder = bundle._library_folder(book_id)  # noqa: SLF001
        if folder is None:
            print(f"  {language}: book_id {book_id} no esta en la biblioteca -- se salta")
            continue

        available = sorted(bundle.formats_in(folder))
        pending = [f for f in BUNDLE_FORMATS if f not in before and f in available]
        print(f"  {language}: en el bundle {before or '(nada)'}")
        print(f"       en la biblioteca: {available}")

        if not pending:
            print("       nada que copiar")
            continue
        print(f"       por copiar: {', '.join(pending)}")

        if not apply:
            changed = True
            continue

        target.mkdir(parents=True, exist_ok=True)
        rescued = bundle._copy_from_library(book_id, target)  # noqa: SLF001

        present = sorted(bundle.formats_in(target))
        entry["files"] = {fmt: str(target) for fmt in present}
        entry["missing"] = [f for f in BUNDLE_FORMATS if f not in present]
        entry["status"] = "completed"
        entry.pop("error", None)
        changed = True
        print(f"       copiados: {', '.join(rescued) or 'ninguno'} -> ahora {len(present)}/{len(BUNDLE_FORMATS)}")

    if apply and changed:
        languages = manifest.get("languages") or {}
        manifest["complete"] = bool(languages) and all(
            not e.get("missing") for e in languages.values()
        )
        tmp = manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(manifest_path)

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Copiar de verdad. Sin esto solo muestra que haria.")
    parser.add_argument("--bundle", default=None,
                        help="Rellenar solo este bundle_id")
    args = parser.parse_args()

    root = Path(config.OUTPUT_DIR) / "bundles"
    if not root.is_dir():
        print(f"No hay bundles en {root}")
        return 0

    manifests = sorted(root.glob(f"*/{MANIFEST_NAME}"))
    if args.bundle:
        manifests = [m for m in manifests if m.parent.name == args.bundle]
    if not manifests:
        print("No hay manifiestos que rellenar.")
        return 0

    print("=" * 72)
    print(f"{'RELLENANDO' if args.apply else 'SIMULACION'} - {len(manifests)} bundle(s)")
    print(f"biblioteca: {Path(config.OUTPUT_DIR) / 'library' / 'objects'}")
    print("=" * 72)

    bundle = create_default_kernel()["bundle"]

    touched = sum(1 for m in manifests if process(m, bundle, args.apply))

    print("\n" + "=" * 72)
    if not args.apply:
        print(f"{touched} bundle(s) se pueden rellenar. Repite con --apply.")
    else:
        print(f"{touched} bundle(s) rellenados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

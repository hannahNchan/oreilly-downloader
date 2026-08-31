"""Download the NLLB-200-3.3B model, already converted to CTranslate2 int8.

    python scripts/download_model.py

Destination comes from app/config.py and lives outside the repository, on
D:. Nothing in the repo tracks it, so it is 3.4 GB you delete by hand when
you want it gone.

Two things this script is careful about:

- Nothing large is allowed to land on C:. huggingface_hub stages downloads
  through a cache under the user profile by default, so HF_HOME is redirected
  next to the destination before the library is imported.
- The tokenizer is fetched from facebook/nllb-200-3.3B, but only the four
  tokenizer files (about 22 MB). That repository also holds the original fp32
  weights, 17.6 GB, which we have no use for.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

# Pre-converted CTranslate2 builds, tried in order. The first that actually has
# a model.bin wins.
MODEL_REPOS = [
    "entai2965/nllb-200-3.3B-ct2-int8",
    "JustFrederik/nllb-200-3.3B-ct2-int8",
    "michaelfeil/ct2fast-nllb-200-3.3B",
]

TOKENIZER_REPO = "facebook/nllb-200-3.3B"
TOKENIZER_FILES = [
    "tokenizer.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer_config.json",
]

CONVERTER_HINT = """
None of the pre-converted repositories could be downloaded. Convert it yourself:

    pip install ctranslate2 "transformers[torch]" sentencepiece
    ct2-transformers-converter ^
        --model facebook/nllb-200-3.3B ^
        --output_dir "{dest}" ^
        --quantization int8_float16 ^
        --copy_files tokenizer.json sentencepiece.bpe.model special_tokens_map.json tokenizer_config.json

What that costs: it downloads the fp32 model (17.6 GB), needs roughly 16 GB of
free RAM while converting, and takes a while. Set HF_HOME to a folder on D:
first so those 17.6 GB do not go to C:.
"""


def megabytes(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / (1024 * 1024)
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024 * 1024)


def redirect_hf_cache(destination: Path) -> None:
    """Keep the staging cache off C:, before huggingface_hub is imported."""
    if os.environ.get("HF_HOME"):
        return
    cache = destination.parent / ".hf-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache)
    print(f"HF_HOME -> {cache}")


def has_model(destination: Path) -> bool:
    return (destination / "model.bin").is_file()


def has_tokenizer(destination: Path) -> bool:
    return (destination / "tokenizer.json").is_file() or (
        destination / "sentencepiece.bpe.model"
    ).is_file()


def download_model(repo: str, destination: Path) -> bool:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError

    print(f"\n--> trying {repo}")
    try:
        snapshot_download(
            repo_id=repo,
            local_dir=str(destination),
            # The repositories hold only the CTranslate2 files; the READMEs and
            # git metadata are the sole exceptions worth skipping.
            ignore_patterns=["*.md", ".gitattributes"],
        )
    except (RepositoryNotFoundError, GatedRepoError) as exc:
        print(f"    not usable: {type(exc).__name__}")
        return False
    except Exception as exc:
        print(f"    failed: {type(exc).__name__}: {exc}")
        return False

    if not has_model(destination):
        print("    downloaded, but there is no model.bin -- not a CTranslate2 model")
        return False

    print(f"    ok: {megabytes(destination):.0f} MB in {destination}")
    return True


def download_tokenizer(destination: Path) -> bool:
    from huggingface_hub import hf_hub_download

    print(f"\n--> tokenizer from {TOKENIZER_REPO} (only the tokenizer files)")
    got_any = False
    for filename in TOKENIZER_FILES:
        target = destination / filename
        if target.is_file():
            print(f"    {filename}: already there")
            got_any = True
            continue
        try:
            hf_hub_download(
                repo_id=TOKENIZER_REPO,
                filename=filename,
                local_dir=str(destination),
            )
            print(f"    {filename}: {megabytes(target):.1f} MB")
            got_any = True
        except Exception as exc:
            # special_tokens_map.json and tokenizer_config.json are not fatal on
            # their own; engine.py falls back to naming the tokenizer class.
            print(f"    {filename}: not available ({type(exc).__name__})")
    return got_any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default=str(config.MODEL_DIR), help="Destination directory")
    parser.add_argument("--repo", default=None, help="Force one specific HF repository")
    parser.add_argument("--force", action="store_true", help="Re-download even if present")
    args = parser.parse_args()

    destination = Path(args.dest)
    print("=" * 72)
    print("NLLB-200-3.3B (CTranslate2, int8_float16)")
    print("=" * 72)
    print(f"Destination: {destination}")

    destination.mkdir(parents=True, exist_ok=True)
    redirect_hf_cache(destination)

    if has_model(destination) and not args.force:
        print(f"\nmodel.bin already present ({megabytes(destination):.0f} MB). "
              f"Use --force to download again.")
    else:
        repos = [args.repo] if args.repo else MODEL_REPOS
        if not any(download_model(repo, destination) for repo in repos):
            print(CONVERTER_HINT.format(dest=destination))
            return 1

    if not has_tokenizer(destination):
        download_tokenizer(destination)
    else:
        print("\n--> tokenizer already present; checking the config files")
        download_tokenizer(destination)

    if not has_tokenizer(destination):
        print("\nFAIL: no tokenizer.json and no sentencepiece.bpe.model. "
              "The service cannot start without one of them.")
        return 1

    print("\n" + "=" * 72)
    print(f"Done. {megabytes(destination):.0f} MB total in {destination}")
    print("Next:  python scripts/verify_cuda.py   then   .\\run.ps1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

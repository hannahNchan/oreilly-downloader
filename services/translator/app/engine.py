"""The translation engine: CTranslate2 + the NLLB tokenizer.

Loaded exactly once, at application startup, and shared by every request.

Concurrency: one lock guards the whole translate operation, tokenisation
included. Two reasons. First, `tokenizer.src_lang` is mutable shared state, so
setting it per request from several threads is a race. Second, there is one GPU
and one client; letting requests overlap would only make them fight over VRAM.
Serialising costs nothing here and removes a class of bug entirely.
"""

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, gpu, postedit, segmenter
from .errors import (
    EngineError,
    GpuOutOfMemoryError,
    ModelNotLoadedError,
    UnsupportedLanguageError,
)
from .languages import pysbd_code

log = logging.getLogger(__name__)

from . import cudaload

# Load-bearing import order. prepare() must run before ctranslate2 is imported:
# CTranslate2 resolves cuBLAS and cuDNN when its extension module loads, and
# Windows does not consult directories added after that. Moving this import to
# the top of the file "for tidiness" breaks GPU support in a way that looks like
# a missing GPU. Do not do it.
CUDA_DLL_DIRS = cudaload.prepare()

import ctranslate2  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

_MB = 1024 * 1024

# Signatures of a CUDA allocation failure. CTranslate2 surfaces these as a plain
# RuntimeError, so the only way to tell OOM from a real bug is the message.
_OOM_MARKERS = (
    "out of memory",
    "cuda_error_out_of_memory",
    "cublas_status_alloc_failed",
    "failed to allocate",
)


@dataclass
class TranslationResult:
    texts: list[str]
    sentences: int
    passed_through: int
    elapsed_ms: int
    source_lang: str
    target_lang: str
    beam_size: int
    postedit_applied: bool


def _is_oom(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _OOM_MARKERS)


class Engine:
    """Owns the CTranslate2 translator, the tokenizer and the post-editor."""

    def __init__(self, translator, tokenizer, editor: postedit.PostEditor, model_vram_mb: int | None):
        self._translator = translator
        self._tokenizer = tokenizer
        self._editor = editor
        self._lock = threading.RLock()
        self._splitters: dict[str, segmenter.SplitSentences] = {}
        self.model_vram_mb = model_vram_mb
        self.model_dir = str(config.MODEL_DIR)
        self.device = config.DEVICE
        self.compute_type = config.COMPUTE_TYPE

    # --- loading ----------------------------------------------------------

    @classmethod
    def load(cls) -> "Engine":
        directory = config.MODEL_DIR
        cls._check_model_dir(directory)
        cls._check_vram()

        before = gpu.free_mb(config.DEVICE_INDEX)

        log.info("loading tokenizer from %s", directory)
        tokenizer = cls._load_tokenizer(directory)
        cls._assert_language_token_order(tokenizer)

        log.info(
            "loading model: device=%s compute_type=%s inter_threads=%d",
            config.DEVICE, config.COMPUTE_TYPE, config.INTER_THREADS,
        )
        started = time.time()
        try:
            translator = ctranslate2.Translator(
                str(directory),
                device=config.DEVICE,
                device_index=config.DEVICE_INDEX,
                compute_type=config.COMPUTE_TYPE,
                inter_threads=config.INTER_THREADS,
                intra_threads=config.INTRA_THREADS,
            )
        except Exception as exc:
            raise ModelNotLoadedError(
                f"CTranslate2 could not load the model: {type(exc).__name__}: {exc}",
                model_dir=str(directory),
                cuda_dll_dirs=CUDA_DLL_DIRS,
                hint="Run scripts/verify_cuda.py to see whether CUDA is visible.",
            ) from exc

        after = gpu.free_mb(config.DEVICE_INDEX)
        # The delta includes the CUDA context (~300 MB), not just the weights.
        # That is the number worth reporting: it is what the process actually
        # costs you.
        footprint = (before - after) if (before is not None and after is not None) else None
        log.info(
            "model loaded in %.1fs; VRAM footprint %s MB; %s MB free",
            time.time() - started, footprint, after,
        )

        editor = postedit.load(config.POSTEDIT_FILE) if config.POSTEDIT_ENABLED else postedit.PostEditor([])
        if editor.size:
            log.info("post-edition rules loaded: %d", editor.size)

        engine = cls(translator, tokenizer, editor, footprint)

        if config.WARMUP:
            engine._warmup()

        return engine

    @staticmethod
    def _check_model_dir(directory: Path) -> None:
        if not directory.is_dir():
            raise ModelNotLoadedError(
                f"Model directory not found: {directory}",
                hint="Run setup.ps1, or scripts/download_model.py on its own.",
            )
        if not (directory / "model.bin").is_file():
            raise ModelNotLoadedError(
                f"No model.bin in {directory}; this is not a CTranslate2 model directory.",
                hint="Run scripts/download_model.py.",
            )
        has_tokenizer = (directory / "tokenizer.json").is_file() or (
            directory / "sentencepiece.bpe.model"
        ).is_file()
        if not has_tokenizer:
            raise ModelNotLoadedError(
                f"No tokenizer files in {directory} (tokenizer.json or sentencepiece.bpe.model).",
                hint="scripts/download_model.py fetches them from facebook/nllb-200-3.3B.",
            )

    @staticmethod
    def _check_vram() -> None:
        """Refuse to start without room, with a message that says why.

        Without this the failure is a CUDA allocation error 20 seconds into
        loading, which says nothing about the 6.6 GB some other process is
        holding.
        """
        if config.DEVICE != "cuda":
            return
        current = gpu.info(config.DEVICE_INDEX)
        if not current.available or current.free_mb is None:
            log.warning("cannot read VRAM (%s); skipping the pre-load check", current.error)
            return
        if current.free_mb < config.VRAM_MIN_FREE_MB:
            raise ModelNotLoadedError(
                f"Only {current.free_mb} MB of VRAM free, {config.VRAM_MIN_FREE_MB} MB needed. "
                f"Something else is holding it: a browser with video takes 1-2 GB, and "
                f"any other model loaded on this GPU keeps its share until unloaded.",
                free_mb=current.free_mb,
                total_mb=current.total_mb,
                required_mb=config.VRAM_MIN_FREE_MB,
            )

    @staticmethod
    def _load_tokenizer(directory: Path):
        """Load the NLLB tokenizer, strictly offline.

        local_files_only matters: without it a missing config file sends
        transformers to huggingface.co, and this service is supposed to work
        with the network unplugged.
        """
        attempts = []
        try:
            return AutoTokenizer.from_pretrained(
                str(directory),
                src_lang=config.DEFAULT_SOURCE_LANG,
                local_files_only=True,
            )
        except Exception as exc:
            attempts.append(f"AutoTokenizer: {type(exc).__name__}: {exc}")

        # Pre-converted CTranslate2 repositories do not always copy
        # tokenizer_config.json, and without it AutoTokenizer cannot work out
        # which class to build. Naming it explicitly recovers that case.
        try:
            from transformers import NllbTokenizerFast

            return NllbTokenizerFast.from_pretrained(
                str(directory),
                src_lang=config.DEFAULT_SOURCE_LANG,
                local_files_only=True,
            )
        except Exception as exc:
            attempts.append(f"NllbTokenizerFast: {type(exc).__name__}: {exc}")

        try:
            from transformers import NllbTokenizer

            return NllbTokenizer.from_pretrained(
                str(directory),
                src_lang=config.DEFAULT_SOURCE_LANG,
                local_files_only=True,
            )
        except Exception as exc:
            attempts.append(f"NllbTokenizer: {type(exc).__name__}: {exc}")

        raise ModelNotLoadedError(
            "Could not load the NLLB tokenizer.", attempts=attempts, model_dir=str(directory)
        )

    @staticmethod
    def _assert_language_token_order(tokenizer) -> None:
        """Fail loudly if the source language token is not where NLLB wants it.

        The HF NLLB tokenizer has moved this token between the start and the end
        of the source sequence across versions (the `legacy_behaviour` flag).
        Getting it wrong does not raise: it produces translations that are merely
        worse, which is the hardest kind of bug to notice. So it is checked once,
        at startup, where it is cheap.
        """
        tokenizer.src_lang = config.DEFAULT_SOURCE_LANG
        tokens = tokenizer.convert_ids_to_tokens(tokenizer("Hello world.").input_ids)
        if not tokens:
            raise ModelNotLoadedError("The tokenizer produced no tokens for a test sentence.")
        if tokens[0] == config.DEFAULT_SOURCE_LANG:
            return
        raise ModelNotLoadedError(
            "The tokenizer puts the source language token in the wrong position: "
            f"expected {config.DEFAULT_SOURCE_LANG} first, got {tokens[:3]}. "
            "This is the legacy_behaviour difference between transformers versions; "
            "install the pinned version from requirements.txt.",
            first_tokens=tokens[:3],
        )

    def _warmup(self) -> None:
        """One tiny translation so the first real request is not the slow one."""
        try:
            started = time.time()
            self.translate_texts(["This is a warm-up sentence."])
            log.info("warm-up done in %.2fs", time.time() - started)
        except Exception as exc:  # never fatal: the service is still usable
            log.warning("warm-up failed (%s); continuing", exc)

    # --- helpers ----------------------------------------------------------

    def _splitter(self, source_lang: str) -> segmenter.SplitSentences:
        splitter = self._splitters.get(source_lang)
        if splitter is None:
            splitter = segmenter.make_pysbd_splitter(pysbd_code(source_lang))
            self._splitters[source_lang] = splitter
        return splitter

    def _counter(self):
        """Token counter with a provable shortcut.

        Every SentencePiece token covers at least one character, and the
        tokenizer adds exactly two specials (language + EOS). So a string whose
        length plus two is already under the target cannot exceed it, and the
        tokenizer never has to run. That skips it for the large majority of
        sentences, which are well under 480 characters.
        """
        target = config.SENTENCE_TOKEN_TARGET
        tokenizer = self._tokenizer

        def count(text: str) -> int:
            cheap = len(text) + 2
            if cheap <= target:
                return cheap
            return len(tokenizer(text).input_ids)

        return count

    def _check_language_token(self, code: str) -> None:
        token_id = self._tokenizer.convert_tokens_to_ids(code)
        if token_id is None or token_id == self._tokenizer.unk_token_id:
            raise UnsupportedLanguageError(
                f"'{code}' is a valid FLORES-200 code but is not in this model's vocabulary."
            )

    # --- translation ------------------------------------------------------

    def translate_texts(
        self,
        texts: list[str],
        source_lang: str | None = None,
        target_lang: str | None = None,
        beam_size: int | None = None,
        apply_postedit: bool | None = None,
    ) -> TranslationResult:
        source_lang = source_lang or config.DEFAULT_SOURCE_LANG
        target_lang = target_lang or config.DEFAULT_TARGET_LANG
        beam_size = beam_size or config.BEAM_SIZE
        started = time.time()

        with self._lock:
            self._check_language_token(source_lang)
            self._check_language_token(target_lang)

            plans, chunks = segmenter.plan(
                texts,
                self._splitter(source_lang),
                self._counter(),
                config.SENTENCE_TOKEN_TARGET,
            )

            passed_through = sum(
                1
                for plan in plans
                for segment in plan
                if isinstance(segment, segmenter.Sentence)
                for chunk in segment.chunks
                if not chunk.translate
            )

            if not chunks:
                # Nothing translatable: whitespace, or a lone URL. Return the
                # input rather than an empty string.
                out = list(texts)
            else:
                translated = self._run_model(chunks, source_lang, target_lang, beam_size)
                out = segmenter.assemble_all(plans, translated)

            do_postedit = (
                config.POSTEDIT_ENABLED if apply_postedit is None else apply_postedit
            ) and target_lang in config.POSTEDIT_LANGS and self._editor.size > 0

            if do_postedit:
                out = [self._editor.apply(text) for text in out]

        return TranslationResult(
            texts=out,
            sentences=len(chunks),
            passed_through=passed_through,
            elapsed_ms=int((time.time() - started) * 1000),
            source_lang=source_lang,
            target_lang=target_lang,
            beam_size=beam_size,
            postedit_applied=bool(do_postedit),
        )

    def _run_model(
        self, chunks: list[str], source_lang: str, target_lang: str, beam_size: int
    ) -> list[str]:
        self._tokenizer.src_lang = source_lang
        # Batch tokenisation, not one call per sentence: same result, far less
        # Python overhead on a chapter with several hundred sentences.
        encoded = self._tokenizer(chunks).input_ids
        tokens = [self._tokenizer.convert_ids_to_tokens(ids) for ids in encoded]
        prefixes = [[target_lang]] * len(tokens)

        results = self._translate_batch(
            tokens, prefixes, beam_size, config.MAX_BATCH_TOKENS
        )

        out: list[str] = []
        for result in results:
            hypothesis = list(result.hypotheses[0]) if result.hypotheses else []
            if hypothesis and hypothesis[0] == target_lang:
                hypothesis = hypothesis[1:]
            ids = self._tokenizer.convert_tokens_to_ids(hypothesis)
            out.append(self._tokenizer.decode(ids, skip_special_tokens=True))
        return out

    def _translate_batch(self, tokens, prefixes, beam_size: int, max_batch_tokens: int):
        try:
            return self._call(tokens, prefixes, beam_size, max_batch_tokens)
        except RuntimeError as exc:
            if not _is_oom(exc):
                raise EngineError(f"CTranslate2 failed: {exc}") from exc

            halved = max(256, max_batch_tokens // 2)
            if halved >= max_batch_tokens:
                raise GpuOutOfMemoryError(
                    "CUDA out of memory and the batch is already at the minimum.",
                    gpu=gpu.info(config.DEVICE_INDEX).as_dict(),
                ) from exc

            log.warning(
                "CUDA OOM at %d tokens/batch; retrying once at %d",
                max_batch_tokens, halved,
            )
            try:
                return self._call(tokens, prefixes, beam_size, halved)
            except RuntimeError as retry_exc:
                if not _is_oom(retry_exc):
                    raise EngineError(f"CTranslate2 failed: {retry_exc}") from retry_exc
                raise GpuOutOfMemoryError(
                    f"CUDA out of memory at {max_batch_tokens} and again at {halved} "
                    f"tokens per batch. Free some VRAM or lower NLLB_MAX_BATCH_TOKENS.",
                    gpu=gpu.info(config.DEVICE_INDEX).as_dict(),
                ) from retry_exc

    def _call(self, tokens, prefixes, beam_size: int, max_batch_tokens: int):
        return self._translator.translate_batch(
            tokens,
            target_prefix=prefixes,
            beam_size=beam_size,
            # Batches are measured in tokens, so a batch of long sentences and a
            # batch of short ones cost the same VRAM. CTranslate2 also sorts by
            # length internally, which keeps the padding down.
            batch_type="tokens",
            max_batch_size=max_batch_tokens,
            max_input_length=config.MAX_INPUT_TOKENS,
            # CTranslate2's default is 256 output tokens. Spanish runs 15-25%
            # longer than English, so a long source sentence would have its
            # translation cut off mid-phrase with no error at all.
            max_decoding_length=config.MAX_DECODING_TOKENS,
            # Put the aligned source token back in place of any <unk>, which is
            # what keeps an unusual identifier from turning into a black hole.
            replace_unknowns=True,
            no_repeat_ngram_size=config.NO_REPEAT_NGRAM_SIZE,
        )

    # --- introspection ----------------------------------------------------

    def stats(self) -> dict:
        return {
            "loaded": True,
            "model_dir": self.model_dir,
            "device": self.device,
            "compute_type": self.compute_type,
            "vram_footprint_mb": self.model_vram_mb,
            "default_beam_size": config.BEAM_SIZE,
            "max_batch_tokens": config.MAX_BATCH_TOKENS,
            "sentence_token_target": config.SENTENCE_TOKEN_TARGET,
            "postedit_rules": self._editor.size,
            "cuda_dll_dirs": CUDA_DLL_DIRS,
        }

    def close(self) -> None:
        with self._lock:
            translator, self._translator = self._translator, None
        del translator

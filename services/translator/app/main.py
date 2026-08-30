"""FastAPI application.

The model is loaded once, in the lifespan handler, and never per request. That
is the whole reason this is a service instead of a library call: 3.6 GB of
weights and a CUDA context are not something you pay for per chapter.

Run it with ONE worker. Each uvicorn worker is a separate process that would
load its own copy of the model, so `--workers 2` on this machine is an
immediate out-of-memory error. run.ps1 sets it explicitly.

The endpoints are `async def` but do their work in a thread, because
CTranslate2's translate_batch is blocking C++ code that would otherwise stall
the event loop and freeze /health while a chapter translates.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import config, gpu, languages
from .engine import Engine
from .errors import (
    BatchTooLargeError,
    EmptyTextError,
    InvalidParameterError,
    ModelNotLoadedError,
    ServiceError,
    TextTooLongError,
    TranslationTimeoutError,
)
from .schemas import (
    BatchTranslateRequest,
    BatchTranslateResponse,
    HealthResponse,
    TranslateRequest,
    TranslateResponse,
)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("nllb")

_engine: Engine | None = None
_load_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model once, before the first request, and release it on exit."""
    global _engine, _load_error
    gpu.init(config.DEVICE_INDEX)
    current = gpu.info(config.DEVICE_INDEX)
    if current.available:
        log.info(
            "GPU %s: %s MB total, %s MB free before loading",
            current.name, current.total_mb, current.free_mb,
        )
    try:
        _engine = Engine.load()
        log.info("ready on http://%s:%d", config.HOST, config.PORT)
    except ServiceError as exc:
        # Starting anyway is deliberate: /health can then explain what is wrong,
        # which beats a process that dies before anything can ask it.
        _load_error = exc.message
        log.error("model not loaded: %s", exc.message)
        if exc.detail:
            log.error("detail: %s", exc.detail)
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        log.exception("unexpected failure while loading the model")

    yield

    if _engine is not None:
        _engine.close()
    gpu.shutdown()


app = FastAPI(
    title="NLLB-200 local translation service",
    description=(
        "English to Latin American Spanish, NLLB-200-3.3B on CTranslate2 int8. "
        "Local only: no network calls after the model is on disk."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content=exc.payload())


# --- validation ------------------------------------------------------------

def _require_engine() -> Engine:
    if _engine is None:
        raise ModelNotLoadedError(
            _load_error or "The model is not loaded.",
            hint="GET /health for the details.",
        )
    return _engine


def _validate_beam(beam_size: int | None) -> None:
    if beam_size is None:
        return
    if beam_size < 1 or beam_size > config.MAX_BEAM_SIZE:
        raise InvalidParameterError(
            f"beam_size must be between 1 and {config.MAX_BEAM_SIZE}, got {beam_size}."
        )


def _validate_single(text: str) -> None:
    if text is None or not text.strip():
        raise EmptyTextError("`text` is empty.")
    if len(text) > config.MAX_TEXT_CHARS:
        raise TextTooLongError(
            f"`text` is {len(text)} characters, the limit is {config.MAX_TEXT_CHARS}. "
            f"Split it, or use /translate/batch.",
            length=len(text),
            limit=config.MAX_TEXT_CHARS,
        )


def _validate_batch(texts: list[str]) -> None:
    if not texts:
        raise EmptyTextError("`texts` is empty.")
    if len(texts) > config.MAX_BATCH_ITEMS:
        raise BatchTooLargeError(
            f"{len(texts)} items, the limit is {config.MAX_BATCH_ITEMS}.",
            count=len(texts),
            limit=config.MAX_BATCH_ITEMS,
        )
    total = sum(len(text or "") for text in texts)
    if total > config.MAX_TEXT_CHARS:
        raise TextTooLongError(
            f"The batch totals {total} characters, the limit is {config.MAX_TEXT_CHARS}.",
            length=total,
            limit=config.MAX_TEXT_CHARS,
        )
    # Individual empty items are NOT an error. A chapter has blank paragraphs and
    # the caller should not have to filter them out; they come back as they went
    # in, which keeps the output aligned with the input by index.
    if all(not (text or "").strip() for text in texts):
        raise EmptyTextError("Every item in `texts` is empty.")


async def _with_timeout(work):
    """Run blocking work in a thread, bounded by REQUEST_TIMEOUT.

    Honest about what this does not do: the timeout answers the *client*. It
    cannot cancel translate_batch, which is C++ code already running in a
    thread, so that work continues to completion and keeps holding the engine
    lock. What actually bounds the work is MAX_TEXT_CHARS.
    """
    try:
        return await asyncio.wait_for(
            run_in_threadpool(work), timeout=config.REQUEST_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        raise TranslationTimeoutError(
            f"Translation exceeded {config.REQUEST_TIMEOUT}s. The GPU work continues "
            f"in the background; send less text per request.",
            timeout_seconds=config.REQUEST_TIMEOUT,
        ) from exc


# --- endpoints -------------------------------------------------------------

@app.post("/translate", response_model=TranslateResponse)
async def translate(payload: TranslateRequest):
    engine = _require_engine()
    _validate_single(payload.text)
    _validate_beam(payload.beam_size)

    source = languages.resolve(payload.source_lang, config.DEFAULT_SOURCE_LANG)
    target = languages.resolve(payload.target_lang, config.DEFAULT_TARGET_LANG)

    result = await _with_timeout(
        lambda: engine.translate_texts(
            [payload.text],
            source_lang=source,
            target_lang=target,
            beam_size=payload.beam_size,
            apply_postedit=payload.postedit,
        )
    )

    return TranslateResponse(
        text=result.texts[0],
        source_lang=result.source_lang,
        target_lang=result.target_lang,
        sentences=result.sentences,
        passed_through=result.passed_through,
        beam_size=result.beam_size,
        postedit_applied=result.postedit_applied,
        elapsed_ms=result.elapsed_ms,
    )


@app.post("/translate/batch", response_model=BatchTranslateResponse)
async def translate_batch(payload: BatchTranslateRequest):
    engine = _require_engine()
    _validate_batch(payload.texts)
    _validate_beam(payload.beam_size)

    source = languages.resolve(payload.source_lang, config.DEFAULT_SOURCE_LANG)
    target = languages.resolve(payload.target_lang, config.DEFAULT_TARGET_LANG)

    result = await _with_timeout(
        lambda: engine.translate_texts(
            list(payload.texts),
            source_lang=source,
            target_lang=target,
            beam_size=payload.beam_size,
            apply_postedit=payload.postedit,
        )
    )

    return BatchTranslateResponse(
        translations=result.texts,
        count=len(result.texts),
        source_lang=result.source_lang,
        target_lang=result.target_lang,
        sentences=result.sentences,
        passed_through=result.passed_through,
        beam_size=result.beam_size,
        postedit_applied=result.postedit_applied,
        elapsed_ms=result.elapsed_ms,
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    current = gpu.info(config.DEVICE_INDEX)

    if _engine is None:
        body = HealthResponse(
            status="error",
            detail=_load_error or "The model is not loaded.",
            model={"loaded": False},
            gpu=current.as_dict(),
            languages=_language_info(),
            limits=_limit_info(),
        )
        return JSONResponse(status_code=503, content=body.model_dump())

    status = "ok"
    detail = None
    if current.available and current.free_mb is not None:
        if current.free_mb < config.VRAM_WARN_FREE_MB:
            status = "degraded"
            detail = (
                f"Only {current.free_mb} MB of VRAM free (warning below "
                f"{config.VRAM_WARN_FREE_MB} MB). A large batch may hit OOM."
            )

    return HealthResponse(
        status=status,
        detail=detail,
        model=_engine.stats(),
        gpu=current.as_dict(),
        languages=_language_info(),
        limits=_limit_info(),
    )


def _language_info() -> dict:
    return {
        "default_source": config.DEFAULT_SOURCE_LANG,
        "default_target": config.DEFAULT_TARGET_LANG,
        "supported_count": len(languages.FLORES),
        "note": "FLORES-200 codes; ISO-639 is accepted and mapped. See app/languages.py.",
    }


def _limit_info() -> dict:
    return {
        "max_text_chars": config.MAX_TEXT_CHARS,
        "max_batch_items": config.MAX_BATCH_ITEMS,
        "max_batch_tokens": config.MAX_BATCH_TOKENS,
        "max_input_tokens": config.MAX_INPUT_TOKENS,
        "max_decoding_tokens": config.MAX_DECODING_TOKENS,
        "request_timeout_seconds": config.REQUEST_TIMEOUT,
        "max_beam_size": config.MAX_BEAM_SIZE,
    }

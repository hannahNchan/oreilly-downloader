"""Service errors and their HTTP mapping.

Every failure the client can cause has its own class with a stable `code`, so
the caller can branch on a string instead of parsing prose. main.py registers a
single handler for ServiceError.
"""


class ServiceError(Exception):
    """Base class. `status` is the HTTP code, `code` the machine-readable tag."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def payload(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class EmptyTextError(ServiceError):
    status = 400
    code = "empty_text"


class TextTooLongError(ServiceError):
    status = 413
    code = "text_too_long"


class BatchTooLargeError(ServiceError):
    status = 413
    code = "batch_too_large"


class UnsupportedLanguageError(ServiceError):
    status = 400
    code = "unsupported_language"


class InvalidParameterError(ServiceError):
    status = 400
    code = "invalid_parameter"


class ModelNotLoadedError(ServiceError):
    status = 503
    code = "model_not_loaded"


class GpuOutOfMemoryError(ServiceError):
    """CUDA ran out of memory even after retrying with a smaller batch."""

    status = 503
    code = "gpu_out_of_memory"


class TranslationTimeoutError(ServiceError):
    status = 504
    code = "timeout"


class EngineError(ServiceError):
    """CTranslate2 failed for a reason that is not OOM."""

    status = 500
    code = "engine_error"

"""GPU memory telemetry through NVML.

One limitation worth knowing before reading /health: on Windows the display
driver runs in WDDM mode, and under WDDM NVML cannot attribute VRAM to a
process. `nvidia-smi --query-compute-apps` lists the processes but reports
`[N/A]` for every one of them. So this module reports the aggregate only:
total, used, free. Enough to know you are near the limit, not enough to name
who took it.

Everything degrades to available=False rather than raising: the service must
still start and translate on a machine where NVML is missing.
"""

import logging
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

_MB = 1024 * 1024

_nvml = None
_handle = None
_error: str | None = None
_initialised = False


@dataclass
class GpuInfo:
    available: bool
    name: str | None = None
    total_mb: int | None = None
    used_mb: int | None = None
    free_mb: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        # Under WDDM this caveat is permanent, so it travels with the payload
        # instead of living only in the docs.
        data["note"] = "aggregate only; WDDM does not expose per-process VRAM"
        return data


def init(device_index: int = 0) -> bool:
    """Initialise NVML once. Safe to call repeatedly."""
    global _nvml, _handle, _error, _initialised
    if _initialised:
        return _handle is not None
    _initialised = True

    try:
        import pynvml
    except ImportError as exc:
        _error = f"pynvml not installed ({exc})"
        return False

    try:
        pynvml.nvmlInit()
        _handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
        _nvml = pynvml
        return True
    except Exception as exc:
        _error = f"{type(exc).__name__}: {exc}"
        log.warning("NVML unavailable: %s", _error)
        _handle = None
        return False


def info(device_index: int = 0) -> GpuInfo:
    """Current aggregate VRAM figures, or available=False with a reason."""
    if not init(device_index):
        return GpuInfo(available=False, error=_error)

    try:
        memory = _nvml.nvmlDeviceGetMemoryInfo(_handle)
        raw_name = _nvml.nvmlDeviceGetName(_handle)
        # nvmlDeviceGetName returns bytes on older nvidia-ml-py, str on newer.
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
        return GpuInfo(
            available=True,
            name=name,
            total_mb=int(memory.total // _MB),
            used_mb=int(memory.used // _MB),
            free_mb=int(memory.free // _MB),
        )
    except Exception as exc:
        return GpuInfo(available=False, error=f"{type(exc).__name__}: {exc}")


def free_mb(device_index: int = 0) -> int | None:
    return info(device_index).free_mb


def shutdown() -> None:
    global _handle, _initialised
    if _nvml is not None and _handle is not None:
        try:
            _nvml.nvmlShutdown()
        except Exception:
            pass
    _handle = None
    _initialised = False

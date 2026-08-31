"""Make the CUDA DLLs findable before CTranslate2 is imported. Windows only.

This is the single most likely thing to go wrong on this platform, so it gets
its own module.

CTranslate2's GPU build needs cuBLAS and cuDNN at load time. On Linux the
`nvidia-*` pip packages put the shared objects somewhere the loader already
looks. On Windows nothing does that for you: the DLLs live wherever they were
installed, and since Python 3.8 the interpreter no longer searches PATH for
extension-module dependencies. If they are not findable, `import ctranslate2`
succeeds and `get_cuda_device_count()` quietly answers 0, which reads exactly
like "no GPU" rather than "missing DLL".

Three places are checked, cheapest first. None of them imports the package it
is probing: `importlib.util.find_spec` gives us the install path without paying
for torch's ~2 second import and its memory.
"""

import importlib.util
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# os.add_dll_directory returns a handle that removes the directory again when
# closed. Keeping the handles referenced here stops the garbage collector from
# undoing the work as soon as prepare() returns.
_handles: list = []
_added: list[str] = []
_done = False


def _spec_dir(module: str) -> Path | None:
    """Installation directory of `module` without importing it."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    if spec.origin and spec.origin != "namespace":
        return Path(spec.origin).parent
    locations = list(spec.submodule_search_locations or [])
    return Path(locations[0]) if locations else None


def _candidates() -> list[Path]:
    found: list[Path] = []

    # 1. Explicit override. Semicolon-separated, Windows PATH style.
    override = os.environ.get("NLLB_CUDA_DLL_DIR", "")
    for part in override.split(";"):
        part = part.strip()
        if part:
            found.append(Path(part))

    # 2. NVIDIA's redistributable wheels, when they ship a win_amd64 build.
    #    Layout is nvidia/<component>/bin on Windows, nvidia/<component>/lib on
    #    Linux; both are cheap to check.
    nvidia_dir = _spec_dir("nvidia")
    if nvidia_dir:
        for pattern in ("*/bin", "*/lib"):
            found.extend(sorted(nvidia_dir.glob(pattern)))

    # 3. PyTorch's CUDA build bundles the same cuBLAS and cuDNN DLLs in
    #    torch/lib. Borrowing them is the reliable Windows path, and it does not
    #    require torch to be imported at all.
    torch_dir = _spec_dir("torch")
    if torch_dir:
        found.append(torch_dir / "lib")

    # 4. A locally installed CUDA Toolkit.
    for variable in ("CUDA_PATH", "CUDA_HOME"):
        root = os.environ.get(variable)
        if root:
            found.append(Path(root) / "bin")

    return found


def prepare() -> list[str]:
    """Add every CUDA DLL directory we can find. Idempotent.

    Returns the directories that were actually added, so verify_cuda.py and
    /health can show which path was taken instead of leaving it a mystery.
    """
    global _done
    if _done:
        return list(_added)
    _done = True

    if os.name != "nt":
        return []

    seen: set[str] = set()
    for candidate in _candidates():
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        try:
            _handles.append(os.add_dll_directory(str(resolved)))
        except OSError as exc:
            log.debug("could not add DLL directory %s: %s", resolved, exc)
            continue
        _added.append(str(resolved))
        log.debug("CUDA DLL directory added: %s", resolved)

    return list(_added)


def added_directories() -> list[str]:
    return list(_added)

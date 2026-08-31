"""Check that CTranslate2 can actually see the GPU, and say why when it cannot.

Run this before downloading 3.5 GB of weights.

The failure this catches is specific to Windows and it is silent: if cuBLAS or
cuDNN are not on the DLL search path, `import ctranslate2` still succeeds and
`get_cuda_device_count()` returns 0. That reads as "no GPU" even on a machine
with a perfectly working 3060, so the diagnosis below distinguishes the two.

    python scripts/verify_cuda.py
"""

import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must run before ctranslate2 is imported. See app/cudaload.py.
from app import cudaload  # noqa: E402

DLL_DIRS = cudaload.prepare()

from app import config, gpu  # noqa: E402


def line(title: str, value) -> None:
    print(f"  {title:<26} {value}")


def main() -> int:
    print("=" * 72)
    print("CTranslate2 / CUDA check")
    print("=" * 72)

    line("Python", f"{sys.version.split()[0]} ({platform.machine()})")
    line("Platform", f"{platform.system()} {platform.release()}")

    print("\nCUDA DLL directories added:")
    if DLL_DIRS:
        for directory in DLL_DIRS:
            print(f"    {directory}")
    else:
        print("    (none -- fine on Linux, a problem on Windows)")

    print()
    try:
        import ctranslate2
    except Exception as exc:
        print(f"  FAIL  cannot import ctranslate2: {type(exc).__name__}: {exc}")
        return 1

    line("ctranslate2", ctranslate2.__version__)

    try:
        device_count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        print(f"  FAIL  get_cuda_device_count(): {type(exc).__name__}: {exc}")
        return 1

    line("CUDA devices seen by CT2", device_count)

    compute_types = set()
    if device_count > 0:
        try:
            compute_types = ctranslate2.get_supported_compute_types("cuda")
        except Exception as exc:
            print(f"  WARN  get_supported_compute_types('cuda'): {exc}")
    line("Supported compute types", ", ".join(sorted(compute_types)) or "(unknown)")

    print("\nGPU (NVML):")
    info = gpu.info(config.DEVICE_INDEX)
    if info.available:
        line("Name", info.name)
        line("Total VRAM", f"{info.total_mb} MB")
        line("Used", f"{info.used_mb} MB")
        line("Free", f"{info.free_mb} MB")
    else:
        line("NVML", f"unavailable ({info.error})")

    print("\n" + "=" * 72)

    if device_count == 0:
        print("RESULT: CTranslate2 sees NO CUDA device.")
        print()
        if info.available:
            print(f"  NVML does see the GPU ({info.name}), so the driver is fine and this")
            print("  is almost certainly the missing cuBLAS/cuDNN DLLs, not the hardware.")
        else:
            print("  NVML cannot see the GPU either. Check the NVIDIA driver first.")
        print()
        print("  Fixes, cheapest first:")
        print("    1. pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
        print("       (works only if NVIDIA published win_amd64 wheels for your CUDA)")
        print("    2. setup.ps1 -WithTorchCuda")
        print("       (installs the PyTorch CUDA build, ~2.5 GB, purely to borrow its")
        print("        cuBLAS/cuDNN DLLs from torch/lib -- torch is never imported)")
        print("    3. Install the CUDA Toolkit + cuDNN for Windows by hand and set")
        print("       CUDA_PATH, or point NLLB_CUDA_DLL_DIR straight at the folder")
        print("       holding cublas64_12.dll and cudnn*.dll")
        return 1

    required = config.COMPUTE_TYPE
    if compute_types and required not in compute_types:
        print(f"RESULT: CUDA works, but compute_type '{required}' is not supported here.")
        print(f"  Available: {', '.join(sorted(compute_types))}")
        print(f"  Set NLLB_COMPUTE_TYPE to one of those (int8_float32 is the usual fallback).")
        return 1

    free = info.free_mb if info.available else None
    print(f"RESULT: OK. CUDA visible, compute_type '{required}' supported.")
    if free is not None:
        print(f"  {free} MB of VRAM free; the model needs about 3600 MB plus working room.")
        if free < config.VRAM_MIN_FREE_MB:
            print(f"  WARNING: below the {config.VRAM_MIN_FREE_MB} MB the service requires at")
            print("  startup. Close whatever else is holding VRAM on this GPU.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

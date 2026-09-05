"""AURA QUANT-X GPU Resource Manager 12.6.2.

Windows laptops often expose Intel integrated graphics as GPU 0 in Task Manager
and NVIDIA as GPU 1. CUDA numbering is independent: the RTX is normally
cuda:0 when it is the only CUDA-capable adapter. This module therefore selects
CUDA by capability, not by Windows Task Manager number.

Policy for 6 GB RTX-class laptops:
- NVIDIA CUDA = AI/voice/quant compute.
- Intel UHD = display/system only; it is not a CUDA peer and cannot add VRAM.
- Voice profile prefers a 3B local LLM to leave VRAM headroom for Whisper+XTTS.
- Diagnostics expose both Windows adapter labels and CUDA adapter details.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

logger = logging.getLogger("aura.gpu_governor")

try:
    import torch  # type: ignore
    TORCH_AVAILABLE = True
except Exception:
    torch = None  # type: ignore
    TORCH_AVAILABLE = False

try:
    import pynvml  # type: ignore
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    pynvml = None  # type: ignore
    NVML_AVAILABLE = False


def _ps_gpu_inventory() -> List[Dict[str, Any]]:
    if platform.system() != "Windows" or not shutil.which("powershell"):
        return []
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion,PNPDeviceID | "
        "ConvertTo-Json -Compress"
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if p.returncode != 0 or not p.stdout.strip():
            return []
        data = json.loads(p.stdout)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []
    except Exception:
        return []


def cuda_info() -> Dict[str, Any]:
    try:
        import torch
    except Exception:
        return {"available": False, "reason": "torch_unavailable"}
    if not torch.cuda.is_available():
        return {"available": False, "reason": "cuda_unavailable", "device_count": 0}
    count = torch.cuda.device_count()
    devices = []
    for idx in range(count):
        props = torch.cuda.get_device_properties(idx)
        devices.append({
            "cudaIndex": idx,
            "name": props.name,
            "vramGB": round(props.total_memory / (1024 ** 3), 2),
            "computeCapability": f"{props.major}.{props.minor}",
        })
    return {"available": True, "device_count": count, "devices": devices}


def resolve_cuda_device() -> str:
    """Return the best CUDA device. NVIDIA is CUDA-capable; Intel UHD is not."""
    info = cuda_info()
    if not info.get("available"):
        return "cpu"
    # Prefer the largest CUDA device; on this laptop this is the RTX 4050.
    best = max(info["devices"], key=lambda x: x["vramGB"])
    os.environ.setdefault("AURA_CUDA_DEVICE", str(best["cudaIndex"]))
    return f"cuda:{best['cudaIndex']}"


def recommended_voice_llm(vram_gb: float) -> str:
    # 6 GB is a concurrency budget, not a reason to load an 8B model alongside
    # XTTS + Whisper. Keep headroom for the voice pipeline.
    if vram_gb >= 10:
        return "llama3.1:8b-instruct-q8_0"
    if vram_gb >= 8:
        return "llama3.1:8b"
    return "llama3.2:3b"


class GPUResourceManager:
    """Defensive VRAM governor for optional CUDA inference.

    The default ceiling is 85% of detected VRAM. The manager never allocates
    tensors, starts drivers, kills processes or installs dependencies.
    Callers that use ``inference_slot`` also coordinate through a small
    process-shared lock file; callers outside the protocol are not preempted.
    """

    def __init__(self, safe_fraction: float = 0.85, lock_path: str | None = None) -> None:
        if not 0.5 <= safe_fraction <= 0.95:
            raise ValueError("safe_fraction must be between 0.5 and 0.95")
        self.safe_fraction = safe_fraction
        self._thread_lock = threading.Lock()
        default_lock = Path(tempfile.gettempdir()) / "aura_quant_x_gpu_inference.lock"
        self.lock_path = Path(lock_path or os.environ.get("AURA_GPU_LOCK_PATH", str(default_lock)))
        self.vram_total_gb = self._detect_total_gb()
        self.vram_safe_limit_gb = round(self.vram_total_gb * self.safe_fraction, 4)

    def _detect_total_gb(self) -> float:
        devices = cuda_info().get("devices") or []
        return float(max((item.get("vramGB", 0.0) for item in devices), default=0.0))

    def _nvml_memory(self) -> tuple[float, float] | None:
        if not NVML_AVAILABLE or pynvml is None:
            return None
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return memory.used / (1024 ** 3), memory.total / (1024 ** 3)
        except Exception:
            return None

    def get_current_vram_usage_gb(self) -> float:
        nvml = self._nvml_memory()
        if nvml is not None:
            return round(nvml[0], 4)
        if TORCH_AVAILABLE and torch is not None:
            try:
                if torch.cuda.is_available():
                    return round(torch.cuda.memory_reserved() / (1024 ** 3), 4)
            except Exception:
                pass
        return 0.0

    def is_safe_to_infer(self, required_gb: float = 1.0) -> bool:
        if required_gb < 0:
            raise ValueError("required_gb must be non-negative")
        if self.vram_safe_limit_gb <= 0:
            return False
        current = self.get_current_vram_usage_gb()
        projected = current + required_gb
        safe = projected <= self.vram_safe_limit_gb
        if not safe:
            logger.warning(
                "VRAM pressure: current=%.2fGB required=%.2fGB limit=%.2fGB",
                current, required_gb, self.vram_safe_limit_gb,
            )
        return safe

    def get_best_device(self, required_gb: float = 1.0) -> str:
        candidate = resolve_cuda_device()
        if candidate == "cpu" or not self.is_safe_to_infer(required_gb):
            return "cpu"
        return candidate

    @contextmanager
    def inference_slot(self, required_gb: float = 1.0) -> Iterator[str]:
        """Reserve an inference slot and release it deterministically."""
        self._thread_lock.acquire()
        lock_handle = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_handle = self.lock_path.open("a+b")
            if lock_handle.seek(0, 2) == 0:
                lock_handle.write(b"0")
                lock_handle.flush()
            lock_handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            yield self.get_best_device(required_gb)
        finally:
            if lock_handle is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    logger.debug("GPU lock release failed", exc_info=True)
                lock_handle.close()
            if TORCH_AVAILABLE and torch is not None:
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
            self._thread_lock.release()

    def health(self, required_gb: float = 1.5) -> Dict[str, Any]:
        current = self.get_current_vram_usage_gb()
        total = self.vram_total_gb
        usage_percent = round((current / total) * 100, 1) if total > 0 else 0.0
        status_name = "NO_GPU" if total <= 0 else ("CRITICAL" if usage_percent > 90 else "WARNING" if usage_percent > 75 else "OK")
        return {
            "status": status_name,
            "usage_percent": usage_percent,
            "used_gb": round(current, 3),
            "total_gb": round(total, 3),
            "safe_limit_gb": round(self.vram_safe_limit_gb, 3),
            "recommended_device": self.get_best_device(required_gb),
            "required_gb": required_gb,
            "cross_process_lock": str(self.lock_path),
        }


GPU_GOVERNOR = GPUResourceManager()


def get_best_inference_device(required_gb: float = 1.5) -> str:
    return GPU_GOVERNOR.get_best_device(required_gb)


def status() -> Dict[str, Any]:
    ci = cuda_info()
    return {
        "platform": platform.platform(),
        "windowsAdapters": _ps_gpu_inventory(),
        "cuda": ci,
        "governor": GPU_GOVERNOR.health(required_gb=1.5),
        "policy": {
            "safeFraction": GPU_GOVERNOR.safe_fraction,
            "taskManagerGPU0": "Intel UHD / display-system",
            "taskManagerGPU1": "NVIDIA RTX / AI compute",
            "cudaDevice": "largest CUDA-capable adapter",
            "note": "Windows GPU numbers are not CUDA device numbers; Intel UHD cannot be combined with RTX VRAM for CUDA."
        },
    }

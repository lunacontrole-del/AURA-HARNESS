"""Voice GPU resource policy, VRAM monitor and diagnostics.

Política AURA (RTX 4050 6 GB híbrida):
- NVIDIA = IA (Whisper + Ollama). Intel UHD = display/sistema.
- NÃO maximizar VRAM à força: encher 6 GB aumenta OOM e latência.
- Maximizar QUALIDADE = usar a GPU com prioridade alta, modelos no sweet spot,
  serializar fases pesadas e manter Ollama residente (keep_alive).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import torch
except Exception:
    torch = None

_GPU_LOCK = threading.RLock()
_VRAM_HISTORY: List[Dict[str, Any]] = []
_HISTORY_MAX = 60  # ~últimos pontos de amostragem


def _nvidia_smi_info() -> Dict[str, Any]:
    """Descobre hardware NVIDIA sem exigir PyTorch no venv da Bridge."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        raw = subprocess.check_output(
            [
                exe,
                "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory,driver_version,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip().splitlines()
        if not raw:
            return {}
        parts = [x.strip() for x in raw[0].split(",")]
        name = parts[0]
        total_mib = float(parts[1]) if len(parts) > 1 else 0.0
        used_mib = float(parts[2]) if len(parts) > 2 else 0.0
        free_mib = float(parts[3]) if len(parts) > 3 else max(0.0, total_mib - used_mib)
        util_gpu = float(parts[4]) if len(parts) > 4 and parts[4] not in ("", "[N/A]") else None
        util_mem = float(parts[5]) if len(parts) > 5 and parts[5] not in ("", "[N/A]") else None
        driver = parts[6] if len(parts) > 6 else None
        temp = float(parts[7]) if len(parts) > 7 and parts[7] not in ("", "[N/A]") else None
        return {
            "nvidiaDetected": True,
            "name": name,
            "vramGB": round(total_mib / 1024.0, 2),
            "usedGB": round(used_mib / 1024.0, 2),
            "freeGB": round(free_mib / 1024.0, 2),
            "utilGpuPct": util_gpu,
            "utilMemPct": util_mem,
            "temperatureC": temp,
            "driver": driver,
        }
    except Exception as exc:
        return {"nvidiaDetected": False, "smiError": str(exc)}


def device_info() -> Dict[str, Any]:
    smi = _nvidia_smi_info()
    if torch is None or not torch.cuda.is_available():
        if smi.get("nvidiaDetected"):
            return {
                **smi,
                "cuda": False,
                "cudaReady": False,
                "device": "cpu",
                "reason": "RTX detectada por nvidia-smi, mas PyTorch CUDA não está disponível neste venv",
            }
        return {"cuda": False, "cudaReady": False, "device": "cpu", "reason": "CUDA indisponível"}
    idx = max(
        range(torch.cuda.device_count()),
        key=lambda i: torch.cuda.get_device_properties(i).total_memory,
    )
    p = torch.cuda.get_device_properties(idx)
    allocated = torch.cuda.memory_allocated(idx) / (1024 ** 3)
    reserved = torch.cuda.memory_reserved(idx) / (1024 ** 3)
    return {
        **smi,
        "nvidiaDetected": True,
        "cuda": True,
        "cudaReady": True,
        "device": f"cuda:{idx}",
        "name": p.name,
        "vramGB": round(p.total_memory / (1024 ** 3), 2),
        "allocatedGB": round(allocated, 2),
        "reservedGB": round(reserved, 2),
    }


def sample_vram(note: str = "") -> Dict[str, Any]:
    """Amostra VRAM/utilização em tempo real e guarda histórico curto."""
    info = device_info()
    point = {
        "ts": time.time(),
        "note": note,
        "usedGB": info.get("usedGB"),
        "freeGB": info.get("freeGB"),
        "allocatedGB": info.get("allocatedGB"),
        "reservedGB": info.get("reservedGB"),
        "utilGpuPct": info.get("utilGpuPct"),
        "utilMemPct": info.get("utilMemPct"),
        "temperatureC": info.get("temperatureC"),
        "vramGB": info.get("vramGB"),
        "name": info.get("name"),
        "cudaReady": info.get("cudaReady"),
    }
    _VRAM_HISTORY.append(point)
    if len(_VRAM_HISTORY) > _HISTORY_MAX:
        del _VRAM_HISTORY[0 : len(_VRAM_HISTORY) - _HISTORY_MAX]
    return point


def vram_snapshot() -> Dict[str, Any]:
    """Snapshot para health/diagnóstico (tempo real + histórico)."""
    current = sample_vram("snapshot")
    hist = list(_VRAM_HISTORY[-12:])
    used_vals = [h["usedGB"] for h in hist if isinstance(h.get("usedGB"), (int, float))]
    util_vals = [h["utilGpuPct"] for h in hist if isinstance(h.get("utilGpuPct"), (int, float))]
    return {
        "current": current,
        "history": hist,
        "peakUsedGB": round(max(used_vals), 2) if used_vals else None,
        "avgUtilGpuPct": round(sum(util_vals) / len(util_vals), 1) if util_vals else None,
        "policy": profile().get("name"),
        "advice": _advice(current),
    }


def _advice(point: Dict[str, Any]) -> str:
    used = point.get("usedGB")
    total = point.get("vramGB") or 0
    util = point.get("utilGpuPct")
    temp = point.get("temperatureC")
    if not total:
        return "Sem NVIDIA detectada — Whisper/Ollama em CPU (qualidade e latência piores)."
    # Térmico primeiro: protege hardware e performance sustentada
    if isinstance(temp, (int, float)):
        if temp >= 87:
            return f"TEMPERATURA CRÍTICA ({temp:.0f}°C). Reduza carga: Whisper base, LLM 3B, pause XTTS; limpe ventilação."
        if temp >= 80:
            return f"GPU quente ({temp:.0f}°C). Performance pode cair por throttle. Monitore e mantenha quant q4 + batch moderado."
    free = point.get("freeGB")
    if isinstance(free, (int, float)) and free < 0.6:
        return "VRAM apertada (<0.6 GB livre). Mantenha LLM 3B q4, Whisper base/small e Piper offline; evite XTTS."
    if isinstance(util, (int, float)) and util < 15 and isinstance(used, (int, float)) and used < 2:
        return "GPU subutilizada. Sistema saudável em idle; carga sobe no STT/LLM. Confirme Ollama keep_alive e Whisper em CUDA."
    if isinstance(util, (int, float)) and util > 85:
        return "GPU sob carga alta — normal durante STT/LLM. Se houver engasgo, serialize fases (já ativo via GPUSlot)."
    if isinstance(temp, (int, float)) and temp >= 70:
        return f"Orçamento OK · GPU {temp:.0f}°C (quente mas estável). Quant q4 recomendada."
    return "Orçamento de VRAM equilibrado para voz de baixa latência."


def cleanup() -> None:
    # Não chamar empty_cache() a cada fala: aumenta latência com realocações.
    return


def profile() -> Dict[str, Any]:
    info = device_info()
    vram = float(info.get("vramGB") or 0)
    # Sweet spot qualidade x estabilidade na 4050 6 GB
    if 0 < vram <= 7:
        return {
            "name": "balanced_voice_6gb",
            "policy": "NVIDIA RTX para IA; Intel UHD para display. Não saturar VRAM.",
            "hardwareDetected": bool(info.get("nvidiaDetected")),
            "cudaReady": bool(info.get("cudaReady", info.get("cuda"))),
            "recommendedLLM": "llama3.2:3b",
            "recommendedWhisper": "small" if vram >= 5.5 else "base",
            "recommendedTTS": "edge_then_piper",
            "xtts": False,
            "vramTargetGB": round(max(1.0, vram - 1.2), 2),
            "maxConcurrentGpuJobs": 1,
        }
    return {
        "name": "full_gpu",
        "policy": "VRAM folgada — pode subir LLM e Whisper",
        "hardwareDetected": bool(info.get("nvidiaDetected")),
        "cudaReady": bool(info.get("cudaReady", info.get("cuda"))),
        "recommendedLLM": "auto",
        "recommendedWhisper": "small",
        "recommendedTTS": "edge_then_piper",
        "xtts": vram >= 10,
        "vramTargetGB": round(max(1.0, vram - 1.5), 2) if vram else 0,
        "maxConcurrentGpuJobs": 2 if vram >= 12 else 1,
    }


class GPUSlot:
    """Serializa fases pesadas de STT/TTS para reduzir picos de VRAM."""

    def __enter__(self):
        _GPU_LOCK.acquire()
        sample_vram("gpu_slot_enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        sample_vram("gpu_slot_exit")
        cleanup()
        _GPU_LOCK.release()

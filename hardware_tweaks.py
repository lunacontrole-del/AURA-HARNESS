"""Ajustes opcionais e reversíveis de processo para o AURA no Windows.

A política padrão é inerte. Nenhum núcleo é presumido como E-Core, nenhum
limite de CPU/GPU é prometido e o garbage collector automático nunca é
desativado pelo módulo. A afinidade só é aplicada quando o operador opta
explicitamente por ela e fornece uma lista validada de CPUs.
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time
from typing import Any, Iterable

try:
    import psutil
except ImportError:  # pragma: no cover - o pacote faz parte dos requirements
    psutil = None  # type: ignore[assignment]


_LOG = logging.getLogger("aura.hardware_tweaks")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_cpu_list(raw: str | None, cpu_count: int | None = None) -> list[int]:
    """Converte uma lista explícita e rejeita CPUs fora do host atual."""
    if not raw:
        return []
    count = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    result: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token or not token.isdigit():
            continue
        index = int(token)
        if 0 <= index < count and index not in result:
            result.append(index)
    return result


class HardwareTweaks:
    """Controla ajustes locais sem tocar na lógica de análise ou trading."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        requested_affinity: Iterable[int] | None = None,
        cleanup_interval_s: float = 1800.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.requested_affinity = [int(item) for item in (requested_affinity or [])]
        self.cleanup_interval_s = max(60.0, float(cleanup_interval_s))
        self._last_cleanup_monotonic = 0.0
        self._cleanup_count = 0
        self._lock = threading.RLock()
        self._affinity_status: dict[str, Any] = {
            "requested": list(self.requested_affinity),
            "applied": False,
            "reason": "disabled_by_default" if not self.enabled else "not_applied",
        }

    @classmethod
    def from_env(cls) -> "HardwareTweaks":
        enabled = _env_flag("AURA_HARDWARE_TWEAKS_ENABLED", False)
        affinity = _parse_cpu_list(os.getenv("AURA_CPU_AFFINITY")) if enabled else []
        try:
            interval = float(os.getenv("AURA_GC_CLEANUP_INTERVAL_S", "1800"))
        except (TypeError, ValueError):
            interval = 1800.0
        return cls(enabled=enabled, requested_affinity=affinity, cleanup_interval_s=interval)

    def apply_all(self) -> dict[str, Any]:
        """Aplica somente políticas opt-in; nunca altera o GC global."""
        if not self.enabled:
            return self.status()
        self.tweak_cpu_affinity()
        return self.status()

    def tweak_cpu_affinity(self) -> dict[str, Any]:
        """Aplica uma lista explícita; não tenta adivinhar P-Cores/E-Cores."""
        with self._lock:
            if not self.enabled:
                self._affinity_status = {
                    "requested": list(self.requested_affinity),
                    "applied": False,
                    "reason": "disabled_by_default",
                }
                return dict(self._affinity_status)
            if not self.requested_affinity:
                self._affinity_status = {
                    "requested": [],
                    "applied": False,
                    "reason": "explicit_affinity_missing",
                }
                return dict(self._affinity_status)
            if psutil is None:
                self._affinity_status = {
                    "requested": list(self.requested_affinity),
                    "applied": False,
                    "reason": "psutil_unavailable",
                }
                return dict(self._affinity_status)
            try:
                process = psutil.Process(os.getpid())
                current = list(process.cpu_affinity())
                allowed = [cpu for cpu in self.requested_affinity if cpu in current]
                if not allowed:
                    self._affinity_status = {
                        "requested": list(self.requested_affinity),
                        "applied": False,
                        "reason": "no_requested_cpu_in_current_affinity",
                    }
                    return dict(self._affinity_status)
                process.cpu_affinity(allowed)
                self._affinity_status = {
                    "requested": list(self.requested_affinity),
                    "applied": True,
                    "effective": allowed,
                    "reason": "explicit_operator_configuration",
                }
            except Exception as exc:
                self._affinity_status = {
                    "requested": list(self.requested_affinity),
                    "applied": False,
                    "reason": f"affinity_error:{type(exc).__name__}",
                }
            return dict(self._affinity_status)

    def force_cleanup_ram(self, *, force: bool = False) -> dict[str, Any]:
        """Executa GC apenas por pedido explícito e com intervalo mínimo."""
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_cleanup_monotonic
            if not force and self._last_cleanup_monotonic and elapsed < self.cleanup_interval_s:
                return {
                    "ok": True,
                    "performed": False,
                    "reason": "cleanup_cooldown",
                    "cleanup_count": self._cleanup_count,
                }
            collected = int(gc.collect())
            self._last_cleanup_monotonic = now
            self._cleanup_count += 1
            return {
                "ok": True,
                "performed": True,
                "collected": collected,
                "cleanup_count": self._cleanup_count,
                "gc_enabled": gc.isenabled(),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "policy": "OPT_IN_EXPLICIT_AFFINITY_NO_GC_DISABLE",
                "gc_enabled": gc.isenabled(),
                "cleanup_count": self._cleanup_count,
                "cleanup_interval_s": self.cleanup_interval_s,
                "affinity": dict(self._affinity_status),
                "execution_allowed": False,
                "advisory_only": True,
            }


__all__ = ["HardwareTweaks", "_parse_cpu_list"]

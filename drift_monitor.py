"""AURA QUANT-X — detector de concept drift para dados de partida.

O componente é local, paper-only e não executa ações externas. O relógio pode
ser injetado para testes determinísticos.
"""
from __future__ import annotations

from collections import deque
from statistics import pvariance
from threading import RLock
from time import time
from typing import Callable, Deque, Dict, Optional


class DriftMonitor:
    def __init__(self, *, maxlen: int = 10, variance_threshold: float = 50.0,
                 cooldown_seconds: float = 180.0,
                 clock: Callable[[], float] = time) -> None:
        if maxlen < 5:
            raise ValueError("maxlen deve ser >= 5")
        if variance_threshold < 0 or cooldown_seconds < 0:
            raise ValueError("threshold e cooldown devem ser nao-negativos")
        self._history: Deque[float] = deque(maxlen=maxlen)
        self.variance_threshold = float(variance_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._block_until = 0.0
        self._lock = RLock()
        self._last_reason: Optional[str] = None

    def record_wom(self, wom_value: float) -> None:
        value = float(wom_value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("wom_value deve ser finito")
        with self._lock:
            self._history.append(value)

    def is_drift_blocked(self) -> bool:
        with self._lock:
            return self._clock() < self._block_until

    def check_drift(self) -> bool:
        with self._lock:
            if self._clock() < self._block_until:
                return True
            if len(self._history) < 5:
                return False
            variance = pvariance(self._history)
            if variance > self.variance_threshold:
                self._block_until = self._clock() + self.cooldown_seconds
                self._last_reason = "CONCEPT_DRIFT_COOLDOWN"
                return True
            return False

    def status(self) -> Dict[str, object]:
        with self._lock:
            now = self._clock()
            return {
                "blocked": now < self._block_until,
                "block_until": self._block_until,
                "remaining_seconds": max(0.0, self._block_until - now),
                "sample_count": len(self._history),
                "variance": pvariance(self._history) if len(self._history) >= 2 else 0.0,
                "threshold": self.variance_threshold,
                "reason": self._last_reason,
            }


DRIFT_MONITOR = DriftMonitor()

__all__ = ["DriftMonitor", "DRIFT_MONITOR"]

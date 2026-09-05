"""Radar local de anomalias de odds; não acessa rede nem executa ordens."""
from __future__ import annotations

from collections import deque
from threading import RLock
from time import time
from typing import Callable, Deque, Dict, Optional, Tuple


class OddsAnomalyDetector:
    def __init__(self, *, window_seconds: float = 60.0,
                 drop_threshold: float = 0.15,
                 min_samples: int = 4,
                 clock: Callable[[], float] = time) -> None:
        if window_seconds <= 0 or not 0 < drop_threshold < 1 or min_samples < 2:
            raise ValueError("parametros invalidos do radar")
        self.window_seconds = float(window_seconds)
        self.drop_threshold = float(drop_threshold)
        self.min_samples = int(min_samples)
        self._clock = clock
        self._history: Deque[Tuple[float, float]] = deque(maxlen=32)
        self._lock = RLock()
        self._last_reason: Optional[str] = None

    def record_odd(self, odd: float, *, timestamp: Optional[float] = None) -> None:
        value = float(odd)
        if value <= 1.0 or value != value or value in (float("inf"), float("-inf")):
            return
        with self._lock:
            self._history.append((self._clock() if timestamp is None else float(timestamp), value))
            self._prune()

    def _prune(self) -> None:
        cutoff = self._clock() - self.window_seconds
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def check_anomaly(self, current_pressure: float) -> bool:
        pressure = float(current_pressure)
        with self._lock:
            self._prune()
            if len(self._history) < self.min_samples:
                return False
            first_odd = self._history[0][1]
            last_odd = self._history[-1][1]
            drop_percent = (first_odd - last_odd) / first_odd
            anomalous = drop_percent > self.drop_threshold and pressure < 0.5
            self._last_reason = "ODDS_MANIPULATION_SUSPECTED" if anomalous else None
            return anomalous

    def status(self, current_pressure: Optional[float] = None) -> Dict[str, object]:
        with self._lock:
            self._prune()
            drop = 0.0
            if len(self._history) >= 2:
                drop = (self._history[0][1] - self._history[-1][1]) / self._history[0][1]
            result = {
                "sample_count": len(self._history),
                "drop_percent": drop,
                "threshold": self.drop_threshold,
                "window_seconds": self.window_seconds,
                "reason": self._last_reason,
            }
            if current_pressure is not None:
                result["anomalous"] = self.check_anomaly(float(current_pressure))
            return result


ODDS_RADAR = OddsAnomalyDetector()

__all__ = ["OddsAnomalyDetector", "ODDS_RADAR"]

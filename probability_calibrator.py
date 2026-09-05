# engine/probability_calibrator.py — V23 isotonic calibration
from __future__ import annotations
import logging
from typing import List

logger = logging.getLogger("aura.calibration")

try:
    from sklearn.isotonic import IsotonicRegression
    _HAS_SK = True
except Exception:
    _HAS_SK = False
    IsotonicRegression = None  # type: ignore


class ProbabilityCalibrator:
    def __init__(self):
        self.model = IsotonicRegression(out_of_bounds="clip") if _HAS_SK else None
        self.is_fitted = False
        self._historical_x: List[float] = []
        self._historical_y: List[float] = []

    def add_outcome(self, raw_prob: float, actual_outcome: bool) -> None:
        self._historical_x.append(float(raw_prob))
        self._historical_y.append(1.0 if actual_outcome else 0.0)
        if len(self._historical_x) >= 20 and len(self._historical_x) % 50 == 0:
            self.fit()

    def fit(self) -> bool:
        if not _HAS_SK or self.model is None or len(self._historical_x) < 20:
            return False
        try:
            self.model.fit(self._historical_x, self._historical_y)
            self.is_fitted = True
            logger.info("Calibracao isotonica retreinada n=%s", len(self._historical_x))
            return True
        except Exception as e:
            logger.warning("Falha fit calibrator: %s", e)
            return False

    def get_calibrated_prob(self, raw_prob: float) -> float:
        p = float(raw_prob or 0.0)
        if not self.is_fitted or self.model is None:
            return p
        try:
            return float(self.model.transform([p])[0])
        except Exception:
            return p


calibrator = ProbabilityCalibrator()

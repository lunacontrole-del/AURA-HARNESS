# engine/ensemble.py — V23 DynamicEnsemble Poisson + Hawkes (paper-trade)
from __future__ import annotations
import logging
from typing import Dict

logger = logging.getLogger("aura.ensemble")


class DynamicEnsemble:
    def __init__(self, poisson_w: float = 0.6, hawkes_w: float = 0.4):
        self.weights: Dict[str, float] = {"poisson": float(poisson_w), "hawkes": float(hawkes_w)}

    def predict(self, poisson_pred: float, hawkes_pred: float) -> float:
        p = float(poisson_pred or 0.0)
        h = float(hawkes_pred or 0.0)
        return (self.weights["poisson"] * p) + (self.weights["hawkes"] * h)

    def update_weights_by_logloss(self, poisson_loss: float, hawkes_loss: float, learning_rate: float = 0.05) -> Dict[str, float]:
        inv_p = 1.0 / (float(poisson_loss) + 1e-6)
        inv_h = 1.0 / (float(hawkes_loss) + 1e-6)
        total = inv_p + inv_h
        new_p = inv_p / total
        new_h = inv_h / total
        self.weights["poisson"] += learning_rate * (new_p - self.weights["poisson"])
        self.weights["hawkes"] += learning_rate * (new_h - self.weights["hawkes"])
        # normalize
        s = self.weights["poisson"] + self.weights["hawkes"]
        if s > 0:
            self.weights["poisson"] /= s
            self.weights["hawkes"] /= s
        logger.debug("Ensemble weights: %s", self.weights)
        return dict(self.weights)


ensemble_model = DynamicEnsemble()

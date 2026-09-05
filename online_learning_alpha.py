# -*- coding: utf-8 -*-
"""
PILAR 10 - Aprendizado Contínuo Online (Alphas)
AURA QUANT-X v12.7.0-RECONSOLIDADO
Consórcio: Chief Quant Architect + AI Compiler
Gradiente via curva sigmoide adaptativa.
Erros em alta confiança = penalidade severa.
Multiplicadores rigidamente em [0.4, 2.5].
Salvamento atômico (write-to-temp + rename).
"""

from __future__ import annotations

import os
import json
import math
import time
import tempfile
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("aura.pilar10.alpha")

ALPHA_MIN = 0.4
ALPHA_MAX = 2.5
ALPHA_DEFAULT = 1.0
CONFIDENCE_HIGH_THRESHOLD = 0.70
CONFIDENCE_LOW_THRESHOLD = 0.55
SIGMOID_STEEPNESS = 10.0
SIGMOID_MIDPOINT = 0.5
LEARNING_RATE = 0.05
KB_PATH_DEFAULT = os.path.join(os.path.dirname(__file__), "artifacts", "kb_weights.json")


@dataclass
class AlphaWeight:
    team_key: str
    weight: float = ALPHA_DEFAULT
    updates: int = 0
    last_update: float = 0.0
    correct: int = 0
    incorrect: int = 0


class OnlineAlphaLearner:
    """
    Sistema de alphas online com curva sigmoide adaptativa.
    Endpoint conceitual: /api/feedback
    """

    def __init__(self, kb_path: str = KB_PATH_DEFAULT):
        self.kb_path = kb_path
        self._weights: Dict[str, AlphaWeight] = {}
        self._lock = threading.Lock()
        self._load()
        logger.info("Alpha Learner ativo | pesos=%d | intervalo=[%.1f, %.1f]",
                    len(self._weights), ALPHA_MIN, ALPHA_MAX)

    def _load(self) -> None:
        if not os.path.exists(self.kb_path):
            return
        try:
            with open(self.kb_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, wd in data.get("weights", {}).items():
                self._weights[key] = AlphaWeight(
                    team_key=key,
                    weight=float(wd.get("weight", ALPHA_DEFAULT)),
                    updates=int(wd.get("updates", 0)),
                    last_update=float(wd.get("last_update", 0)),
                    correct=int(wd.get("correct", 0)),
                    incorrect=int(wd.get("incorrect", 0)),
                )
        except Exception as exc:
            logger.error("Falha ao carregar kb_weights: %s", exc)

    def _clamp(self, value: float) -> float:
        """Limita rigidamente ao intervalo de segurança [0.4, 2.5]."""
        return max(ALPHA_MIN, min(ALPHA_MAX, value))

    def _sigmoid(self, x: float) -> float:
        """Curva sigmoide: 1 / (1 + exp(-steepness * (x - midpoint)))"""
        exponent = -SIGMOID_STEEPNESS * (x - SIGMOID_MIDPOINT)
        # Proteção contra overflow
        if exponent > 20:
            return 0.0
        if exponent < -20:
            return 1.0
        return 1.0 / (1.0 + math.exp(exponent))

    def _calculate_sigmoid_adjustment(
        self,
        confidence: float,
        is_correct: bool,
        learning_rate: float = LEARNING_RATE,
    ) -> float:
        """
        Calcula ajuste usando curva sigmoide adaptativa.
        Erros em alta confiança → penalidade severa.
        """
        direction = 1.0 if is_correct else -1.0
        sigmoid_value = self._sigmoid(confidence)
        adjustment = learning_rate * sigmoid_value * direction

        # Penalidade severa para erros em alta confiança
        if not is_correct and confidence > CONFIDENCE_HIGH_THRESHOLD:
            severity = 1.0 + (confidence - CONFIDENCE_HIGH_THRESHOLD) * 25.0
            adjustment *= severity
            logger.warning(
                "Penalidade severa aplicada | conf=%.3f | severity=%.2f",
                confidence, severity,
            )
        return adjustment

    def feedback(
        self,
        team_key: str,
        confidence: float,
        is_correct: bool,
    ) -> Dict[str, Any]:
        """
        Endpoint de feedback (/api/feedback).
        Atualiza peso do time e persiste atomicamente.
        """
        with self._lock:
            if team_key not in self._weights:
                self._weights[team_key] = AlphaWeight(team_key=team_key)

            alpha = self._weights[team_key]
            old_weight = alpha.weight
            adj = self._calculate_sigmoid_adjustment(confidence, is_correct)
            new_weight = self._clamp(old_weight + adj)

            alpha.weight = new_weight
            alpha.updates += 1
            alpha.last_update = time.time()
            if is_correct:
                alpha.correct += 1
            else:
                alpha.incorrect += 1

            self._atomic_save()

            return {
                "team_key": team_key,
                "old_weight": round(old_weight, 4),
                "new_weight": round(new_weight, 4),
                "adjustment": round(adj, 6),
                "confidence": confidence,
                "is_correct": is_correct,
                "updates": alpha.updates,
            }

    def get_weight(self, team_key: str) -> float:
        with self._lock:
            if team_key in self._weights:
                return self._weights[team_key].weight
            return ALPHA_DEFAULT

    def _atomic_save(self) -> None:
        """Salvamento atômico: write-to-temp + rename."""
        os.makedirs(os.path.dirname(self.kb_path) or ".", exist_ok=True)
        payload = {
            "version": "12.7.0-RECONSOLIDADO",
            "updated_at": time.time(),
            "weights": {
                k: asdict(v) for k, v in self._weights.items()
            },
        }
        dir_name = os.path.dirname(self.kb_path) or "."
        fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.kb_path)  # atômico
        except Exception as exc:
            logger.error("Falha no save atômico: %s", exc)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "teams": len(self._weights),
                "weights": {k: round(v.weight, 4) for k, v in self._weights.items()},
            }


_learner: Optional[OnlineAlphaLearner] = None
_learner_lock = threading.Lock()


def get_alpha_learner() -> OnlineAlphaLearner:
    global _learner
    with _learner_lock:
        if _learner is None:
            _learner = OnlineAlphaLearner()
        return _learner


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    al = get_alpha_learner()
    print(al.feedback("TIME_A", 0.85, True))
    print(al.feedback("TIME_A", 0.92, False))  # penalidade severa
    print(al.feedback("TIME_B", 0.60, True))
    print("Stats:", al.stats())
    print("Pilar 10 validado.")


@dataclass
class FeedbackResult:
    success: bool
    team_name: str
    outcome: str
    old_weight: float
    new_weight: float
    weight_delta: float
    clamped: bool = False
    error: Optional[str] = None


@dataclass
class _CompatTeamState:
    team_name: str
    weight: float = ALPHA_DEFAULT
    total_feedback: int = 0
    correct_count: int = 0
    incorrect_count: int = 0
    last_updated: float = 0.0
    last_outcome: Optional[str] = None
    confidence_history: List[float] = field(default_factory=list)


class OnlineLearningAlphaSystem:
    """Contrato completo do anexo, usando a mesma curva do learner canônico."""

    def __init__(self, weights_file: str = KB_PATH_DEFAULT):
        self.weights_file = weights_file
        self._weights: Dict[str, _CompatTeamState] = {}
        self._lock = threading.RLock()
        self._save_interval = 5.0
        self._last_save = 0.0
        self._load_compat()

    def _load_compat(self) -> None:
        if not os.path.exists(self.weights_file):
            return
        try:
            with open(self.weights_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            source = data.get("teams") or data.get("weights") or {}
            for key, raw in source.items():
                raw = raw if isinstance(raw, dict) else {}
                self._weights[str(key)] = _CompatTeamState(
                    team_name=str(raw.get("team_name") or key),
                    weight=float(raw.get("weight", ALPHA_DEFAULT)),
                    total_feedback=int(raw.get("total_feedback", raw.get("updates", 0))),
                    correct_count=int(raw.get("correct_count", raw.get("correct", 0))),
                    incorrect_count=int(raw.get("incorrect_count", raw.get("incorrect", 0))),
                    last_updated=float(raw.get("last_updated", raw.get("last_update", 0.0))),
                    last_outcome=raw.get("last_outcome"),
                    confidence_history=list(raw.get("confidence_history") or []),
                )
        except Exception as exc:
            logger.error("Falha ao carregar learner compatível: %s", exc)

    def _calculate_sigmoid_adjustment(self, confidence: float, is_correct: bool, learning_rate: float = LEARNING_RATE) -> float:
        confidence = max(0.0, min(1.0, float(confidence)))
        sigmoid = 1.0 / (1.0 + math.exp(-SIGMOID_STEEPNESS * (confidence - SIGMOID_MIDPOINT)))
        adjustment = float(learning_rate) * sigmoid * (1.0 if is_correct else -1.0)
        if not is_correct and confidence > CONFIDENCE_HIGH_THRESHOLD:
            adjustment *= 1.0 + (confidence - CONFIDENCE_HIGH_THRESHOLD) * 25.0
        return adjustment

    def _clamp_weight(self, value: float) -> Tuple[float, bool]:
        clamped = max(ALPHA_MIN, min(ALPHA_MAX, float(value)))
        return clamped, clamped != float(value)

    def process_feedback(self, team_name: str, outcome: str, confidence: float) -> FeedbackResult:
        key = str(team_name or "unknown")
        normalized = str(outcome or "").lower()
        if normalized not in {"correct", "incorrect", "partial"}:
            return FeedbackResult(False, key, normalized, self.get_weight(key), self.get_weight(key), 0.0, error="invalid_outcome")
        with self._lock:
            state = self._weights.setdefault(key, _CompatTeamState(team_name=key))
            old = float(state.weight)
            is_correct = normalized == "correct"
            factor = 0.5 if normalized == "partial" else 1.0
            delta = self._calculate_sigmoid_adjustment(confidence, is_correct) * factor
            new, clamped = self._clamp_weight(old + delta)
            state.weight = new
            state.total_feedback += 1
            if normalized == "correct":
                state.correct_count += 1
            elif normalized == "incorrect":
                state.incorrect_count += 1
            state.last_updated = time.time()
            state.last_outcome = normalized
            state.confidence_history = (state.confidence_history + [float(confidence)])[-50:]
            result = FeedbackResult(True, key, normalized, old, new, new - old, clamped)
            if self._save_interval <= 0 or time.time() - self._last_save >= self._save_interval:
                self._atomic_save_compat()
            return result

    def process_feedback_multi_team(self, teams: List[str], outcome: str, confidence: float) -> List[FeedbackResult]:
        return [self.process_feedback(team, outcome, confidence) for team in list(teams or [])]

    def get_weight(self, team_name: str) -> float:
        with self._lock:
            state = self._weights.get(str(team_name or ""))
            return float(state.weight) if state else ALPHA_DEFAULT

    def get_team_stats(self, team_name: str) -> Dict[str, Any]:
        with self._lock:
            state = self._weights.get(str(team_name or ""))
            if state is None:
                return {"team_name": str(team_name or ""), "weight": ALPHA_DEFAULT, "total_feedback": 0, "correct_count": 0, "incorrect_count": 0, "win_rate": 0.0}
            return {
                "team_name": state.team_name,
                "weight": state.weight,
                "total_feedback": state.total_feedback,
                "correct_count": state.correct_count,
                "incorrect_count": state.incorrect_count,
                "win_rate": state.correct_count / state.total_feedback if state.total_feedback else 0.0,
                "last_outcome": state.last_outcome,
            }

    def _atomic_save_compat(self) -> None:
        os.makedirs(os.path.dirname(self.weights_file) or ".", exist_ok=True)
        def serialize(value: Any) -> Dict[str, Any]:
            if hasattr(value, "__dataclass_fields__"):
                return asdict(value)
            return {name: getattr(value, name, None) for name in ("team_name", "weight", "total_feedback", "correct_count", "incorrect_count", "last_updated", "last_outcome", "confidence_history")}
        payload = {
            "version": "12.7.0-RECONSOLIDADO",
            "updated_at": time.time(),
            "teams": {key: serialize(value) for key, value in self._weights.items()},
        }
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=os.path.dirname(self.weights_file) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.weights_file)
            self._last_save = time.time()
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def force_save(self) -> None:
        with self._lock:
            self._atomic_save_compat()

"""Aprendizagem IMC exclusivamente pós-partida e advisory-only.

Não participa no caminho de decisão durante a partida. Atualizações exigem
um outcome final explícito e são persistidas no banco canónico do Engine.
"""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

DEFAULT_XG_MULT = 5.0
DEFAULT_PRESSURE_MULT = 1.0
MIN_XG_MULT, MAX_XG_MULT = 0.5, 20.0
MIN_PRESSURE_MULT, MAX_PRESSURE_MULT = 0.1, 5.0
LEARNING_RATE = 0.2


def _finite(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


class IMCPostMatchLearner:
    """Ajusta pesos somente depois de um resultado final validado."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else Path(__file__).resolve().parent / "aura_quant_x.db"
        self._lock = RLock()
        self.weights = {"xg_multiplier": DEFAULT_XG_MULT, "pressure_multiplier": DEFAULT_PRESSURE_MULT}
        self._ensure_schema()
        self.load_weights()

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS imc_learning_state ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), xg_multiplier REAL NOT NULL, "
                "pressure_multiplier REAL NOT NULL, updates INTEGER NOT NULL DEFAULT 0, "
                "updated_at REAL NOT NULL)"
            )
            connection.commit()

    def _safe_weights(self, values: Mapping[str, Any]) -> dict[str, float]:
        xg = _finite(values.get("xg_multiplier"), DEFAULT_XG_MULT)
        pressure = _finite(values.get("pressure_multiplier"), DEFAULT_PRESSURE_MULT)
        return {
            "xg_multiplier": max(MIN_XG_MULT, min(MAX_XG_MULT, xg)),
            "pressure_multiplier": max(MIN_PRESSURE_MULT, min(MAX_PRESSURE_MULT, pressure)),
        }

    def load_weights(self) -> dict[str, float]:
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as connection:
                    row = connection.execute(
                        "SELECT xg_multiplier, pressure_multiplier FROM imc_learning_state WHERE id = 1"
                    ).fetchone()
                if row:
                    self.weights = self._safe_weights({"xg_multiplier": row[0], "pressure_multiplier": row[1]})
            except (OSError, sqlite3.Error, TypeError, ValueError):
                self.weights = {"xg_multiplier": DEFAULT_XG_MULT, "pressure_multiplier": DEFAULT_PRESSURE_MULT}
            return self.weights.copy()

    def process_end_of_match(self, match_data: Mapping[str, Any], *, finalized: bool = False) -> dict[str, Any]:
        """Processa apenas outcome final; nunca aceita telemetria em andamento."""
        if not finalized:
            return {"ok": False, "status": "BLOCKED", "reason": "final_outcome_required", "weights": self.get_current_weights()}
        predicted = _finite(match_data.get("predicted_imc"), 0.0)
        actual = _finite(match_data.get("actual_corners"), -1.0)
        if predicted <= 0.0 or actual < 0.0 or predicted > 1_000_000 or actual > 1_000_000:
            return {"ok": False, "status": "BLOCKED", "reason": "invalid_outcome", "weights": self.get_current_weights()}
        with self._lock:
            error = actual - (predicted / 2.0)
            updated = self._safe_weights({
                "xg_multiplier": self.weights["xg_multiplier"] - error * LEARNING_RATE,
                "pressure_multiplier": self.weights["pressure_multiplier"] - error * (LEARNING_RATE / 5.0),
            })
            self.weights = updated
            try:
                with sqlite3.connect(self.db_path) as connection:
                    connection.execute(
                        "INSERT INTO imc_learning_state (id, xg_multiplier, pressure_multiplier, updates, updated_at) "
                        "VALUES (1, ?, ?, 1, ?) ON CONFLICT(id) DO UPDATE SET "
                        "xg_multiplier=excluded.xg_multiplier, pressure_multiplier=excluded.pressure_multiplier, "
                        "updates=imc_learning_state.updates + 1, updated_at=excluded.updated_at",
                        (updated["xg_multiplier"], updated["pressure_multiplier"], time.time()),
                    )
                    connection.commit()
            except (OSError, sqlite3.Error):
                return {"ok": False, "status": "WARNING", "reason": "persistence_failed", "weights": self.weights.copy()}
            return {"ok": True, "status": "LEARNED_POST_MATCH", "weights": self.weights.copy(), "execution_allowed": False, "paper_trade": True}

    def get_current_weights(self) -> dict[str, float]:
        with self._lock:
            return self.weights.copy()

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "mode": "POST_MATCH_ONLY", "weights": self.get_current_weights(), "paper_trade": True, "execution_allowed": False}


__all__ = ["IMCPostMatchLearner"]

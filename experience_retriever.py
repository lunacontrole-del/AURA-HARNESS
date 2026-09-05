# engine/agents/experience_retriever.py
"""AURA QUANT-X V25 — Experience Retriever (RAG leve sobre ExperienceDB).

Mistura probabilidade matematica pura com historico empirico.
Invariante: paper_trade / advisory-only. Nao executa ordem.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("aura.retriever")

try:
    from core.experience_db import ExperienceDB
except ImportError:
    from engine.core.experience_db import ExperienceDB


class ExperienceRetrieverAgent:
    def __init__(
        self,
        db_path: str = "engine/experience_memory.db",
        calibration_weight: float = 0.70,
    ):
        self.db = ExperienceDB(db_path)
        # Peso da experiencia quando confidence MEDIUM/HIGH (resto = matematica)
        self.calibration_weight = float(calibration_weight)

    def get_context_for_decision(
        self,
        current_imc: float,
        current_minute: int,
        is_pre_corner: bool = False,
    ) -> dict:
        imc_rounded = round(float(current_imc or 0.0), 1)
        minute = int(current_minute or 0)
        minute_bucket = (minute // 15) * 15

        filters = {
            "imc_min": imc_rounded - 0.5,
            "imc_max": imc_rounded + 0.5,
            "minute_min": minute_bucket,
            "minute_max": minute_bucket + 14,
            "is_pre_corner": 1 if is_pre_corner else 0,
        }

        history = self.db.query_experience(filters)

        if history.get("confidence") in ("MEDIUM", "HIGH"):
            logger.info(
                "Contexto historico: IMC %s min %s sucesso %.1f%% (%s casos)",
                imc_rounded,
                minute,
                history.get("success_rate", 0.0),
                history.get("total_cases", 0),
            )
            return history

        return {
            "total_cases": 0,
            "success_rate": 0.0,
            "confidence": "NONE",
            "paper_trade": True,
        }

    def calculate_blended_probability(
        self, raw_math_prob: float, context_data: dict
    ) -> float:
        """
        Mistura matematica pura (Poisson/Hawkes/MC) com historico.
        Sem dados suficientes -> 100% matematica.
        """
        try:
            raw = float(raw_math_prob)
        except (TypeError, ValueError):
            raw = 0.0
        # normaliza se veio em percentual
        if raw > 1.0:
            raw = raw / 100.0
        raw = max(0.0, min(1.0, raw))

        confidence = (context_data or {}).get("confidence", "NONE")
        hist = (context_data or {}).get("success_rate", 0.0)
        try:
            hist_f = float(hist)
        except (TypeError, ValueError):
            hist_f = 0.0
        if hist_f > 1.0:
            hist_f = hist_f / 100.0
        hist_f = max(0.0, min(1.0, hist_f))

        if confidence in ("MEDIUM", "HIGH"):
            w = self.calibration_weight
            final = (raw * (1.0 - w)) + (hist_f * w)
            return round(max(0.0, min(1.0, final)), 6)
        return raw

    def record_snapshot(
        self,
        fixture_id: str,
        state: dict,
        is_corner: bool = False,
        is_pre_corner: bool = False,
    ) -> None:
        self.db.schedule_write(fixture_id, state, is_corner, is_pre_corner)

    def flush(self) -> int:
        return self.db.flush_writes()

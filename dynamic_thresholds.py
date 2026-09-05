#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DynamicThresholds — barreiras ENTRA adaptativas a partir da calibracao.
Paper trade only. Nunca executa ordem.

Fontes (em ordem de prioridade):
1. threshold_proposal.json (se voce aprovou: {"approved": true, ...})
2. MemoryStore.calibration_report() (ajuste automatico conservador)
3. Defaults seguros (score>=70, conf>=0.75)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("aura.dyn_thresholds")


@dataclass
class Thresholds:
    min_score: float = 70.0
    min_confidence: float = 0.75
    min_triggers: int = 2
    min_excitation: float = 0.30
    source: str = "default"
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DynamicThresholds:
    """Singleton leve: get() retorna barreiras atuais."""

    def __init__(
        self,
        proposal_path: Optional[Path] = None,
        memory_path: Optional[Path] = None,
        refresh_sec: float = 300.0,
        base_score: float = 70.0,
        base_conf: float = 0.75,
    ):
        root = Path(__file__).resolve().parents[1]
        self.proposal_path = proposal_path or (
            root / "data" / "daily_learning" / "threshold_proposal.json"
        )
        self.memory_path = memory_path or (root / "data" / "glm_memory.json")
        self.refresh_sec = float(refresh_sec)
        self.base_score = float(base_score)
        self.base_conf = float(base_conf)
        self._lock = threading.Lock()
        self._current = Thresholds(
            min_score=self.base_score,
            min_confidence=self.base_conf,
            source="default",
            updated_at=time.time(),
        )
        self._last_load = 0.0

    def get(self) -> Thresholds:
        now = time.time()
        if now - self._last_load >= self.refresh_sec:
            self.refresh()
        with self._lock:
            return Thresholds(**asdict(self._current))

    def refresh(self) -> Thresholds:
        th = self._compute()
        with self._lock:
            self._current = th
            self._last_load = time.time()
        logger.info(
            "thresholds source=%s score>=%.1f conf>=%.3f",
            th.source,
            th.min_score,
            th.min_confidence,
        )
        return th

    def _compute(self) -> Thresholds:
        # 1) proposta APROVADA manualmente
        approved = self._load_approved_proposal()
        if approved is not None:
            return approved

        # 2) ajuste automatico conservador pela calibracao
        auto = self._from_calibration()
        if auto is not None:
            return auto

        # 3) default
        return Thresholds(
            min_score=self.base_score,
            min_confidence=self.base_conf,
            source="default",
            updated_at=time.time(),
        )

    def _load_approved_proposal(self) -> Optional[Thresholds]:
        if not self.proposal_path.exists():
            return None
        try:
            data = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not data.get("approved"):
            return None
        sug = data.get("suggested") or data.get("current") or {}
        try:
            return Thresholds(
                min_score=float(sug.get("min_score", self.base_score)),
                min_confidence=float(sug.get("min_confidence", self.base_conf)),
                min_triggers=int(sug.get("min_triggers", 2)),
                min_excitation=float(sug.get("min_excitation", 0.30)),
                source="approved_proposal",
                updated_at=time.time(),
            )
        except (TypeError, ValueError):
            return None

    def _from_calibration(self) -> Optional[Thresholds]:
        """Ajuste automatico limitado (±5 score / ±0.05 conf)."""
        if not self.memory_path.exists():
            return None
        try:
            mem = json.loads(self.memory_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        calib = (mem or {}).get("calibration") or {}
        preds = int(calib.get("predictions") or 0)
        correct = int(calib.get("correct") or 0)
        if preds < 20:
            return None  # cold start: nao mexer

        hit_rate = correct / preds if preds else 0.0
        score = self.base_score
        conf = self.base_conf
        # hit_rate aqui e sobre outcomes de ENTRA registrados
        if hit_rate < 0.45:
            score = min(90.0, self.base_score + 5)
            conf = min(0.90, self.base_conf + 0.05)
            src = "auto_strict"
        elif hit_rate >= 0.70:
            score = max(60.0, self.base_score - 3)
            conf = max(0.60, self.base_conf - 0.03)
            src = "auto_relax"
        else:
            src = "auto_hold"
            score, conf = self.base_score, self.base_conf

        # bandas de confianca: se banda 0.8 tem actual << declared, endurece
        bands = calib.get("by_confidence_band") or {}
        for band, v in bands.items():
            try:
                n = int(v.get("n", 0))
                if n < 15:
                    continue
                declared = float(band)
                actual = int(v.get("hits", 0)) / n
                if declared >= 0.8 and actual < declared - 0.15:
                    conf = max(conf, min(0.90, declared))
                    score = max(score, self.base_score + 3)
                    src = "auto_recalibrate"
            except (TypeError, ValueError, ZeroDivisionError):
                continue

        return Thresholds(
            min_score=round(score, 1),
            min_confidence=round(conf, 3),
            source=src,
            updated_at=time.time(),
        )

    def allows_enter(
        self,
        *,
        score: float,
        confidence: float,
        triggers: Optional[list] = None,
        excitation: Optional[float] = None,
        kills: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Gate final: ENTRA so se passar nas barreiras atuais."""
        th = self.get()
        reasons = []
        ok = True
        if kills:
            ok = False
            reasons.append(f"kills={list(kills)[:5]}")
        if float(score or 0) < th.min_score:
            ok = False
            reasons.append(f"score {score} < {th.min_score}")
        if float(confidence or 0) < th.min_confidence:
            ok = False
            reasons.append(f"conf {confidence} < {th.min_confidence}")
        ntrig = len(triggers or [])
        if ntrig < th.min_triggers:
            ok = False
            reasons.append(f"triggers {ntrig} < {th.min_triggers}")
        if excitation is not None and float(excitation) < th.min_excitation:
            ok = False
            reasons.append(f"excitation {excitation} < {th.min_excitation}")

        return {
            "allow": ok,
            "thresholds": th.to_dict(),
            "reasons": reasons,
            "paper_trade": True,
            "execution_allowed": False,
        }


# instancia global
_DYN: Optional[DynamicThresholds] = None


def get_dynamic_thresholds() -> DynamicThresholds:
    global _DYN
    if _DYN is None:
        _DYN = DynamicThresholds()
        _DYN.refresh()
    return _DYN

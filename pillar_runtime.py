"""Adaptadores dos dez pilares para os entrypoints oficiais do AURA QUANT-X.

Este módulo não cria um segundo servidor nem uma segunda política de risco. Ele
oferece uma única instância lazy para ligar as implementações dos anexos aos
fluxos existentes, mantendo o banco definido por AURA_DB_PATH e paper trade.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from deterministic_router import DeterministicRouter, get_router
from hybrid_data_store import HybridDataStore, TelemetryRecord
from online_learning_alpha import OnlineAlphaLearner
from poisson_risk_engine import RiskDecision, RiskManager, get_risk_manager
from structured_observability import EventLevel, StructuredObservability

logger = logging.getLogger("aura.pillar_runtime")
ROOT = Path(__file__).resolve().parent
CANONICAL_DB = os.environ.get("AURA_DB_PATH", str(ROOT / "aura_quant_x.db"))
EVENT_LOG_DIR = os.environ.get("AURA_EVENT_LOG_DIR", str(ROOT / "artifacts" / "logs"))
ALPHA_PATH = os.environ.get("AURA_ONLINE_ALPHA_PATH", str(ROOT / "artifacts" / "kb_weights_online.json"))


def _ensure_canonical_schema() -> None:
    """Cria o schema P0 antes de qualquer callback de flush."""
    try:
        from data_store import init_schema
        init_schema(CANONICAL_DB)
    except Exception as exc:
        logger.exception("Schema canônico indisponível para pilares: %s", exc)
        raise


def _persist_hybrid_records(records: List[TelemetryRecord]) -> None:
    """Persiste lotes do Pilar 1 em `logs_telemetria` sem substituir o SQLite."""
    if not records:
        return
    _ensure_canonical_schema()
    conn = sqlite3.connect(CANONICAL_DB, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("""CREATE TABLE IF NOT EXISTS hybrid_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            odds REAL NOT NULL,
            odds_velocity REAL NOT NULL,
            asian_line REAL NOT NULL,
            corner_count INTEGER NOT NULL,
            market_type TEXT NOT NULL,
            extra_json TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (unixepoch())
        )""")
        conn.executemany(
            """INSERT INTO hybrid_telemetry
               (fixture_id, timestamp, odds, odds_velocity, asian_line,
                corner_count, market_type, extra_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(record.match_id),
                    float(record.timestamp),
                    float(record.odds),
                    float(record.odds_velocity),
                    float(record.asian_line),
                    int(record.corner_count),
                    str(record.market_type),
                    json.dumps(record.extra, ensure_ascii=False, default=str),
                )
                for record in records
            ],
        )
        conn.commit()
    finally:
        conn.close()


class PillarRuntime:
    """Fachada única dos módulos dos anexos no caminho de produção."""

    def __init__(self) -> None:
        self.router: DeterministicRouter = get_router()
        self.risk: RiskManager = get_risk_manager()
        self.observability = StructuredObservability(log_dir=EVENT_LOG_DIR)
        self.alpha = OnlineAlphaLearner(ALPHA_PATH)
        self.hybrid_store = HybridDataStore(
            disk_path=CANONICAL_DB,
            buffer_limit=int(os.environ.get("AURA_HYBRID_BUFFER_LIMIT", "10000")),
            flush_interval=float(os.environ.get("AURA_HYBRID_FLUSH_INTERVAL", "60")),
            persist_callback=_persist_hybrid_records,
        )
        self._closed = False
        logger.info("Pilares integrados: router, hybrid_store, p5_risk, observability, online_alpha")

    def route(self, message: str) -> Dict[str, Any]:
        return self.router.classify(message)

    def log_event(
        self,
        event: str,
        *,
        method: str = "INTERNAL",
        route: str = "pillar_runtime",
        message: str = "",
        duration_ms: float = 0.0,
        level: EventLevel = EventLevel.INFO,
        data_integrity: str = "ok",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            self.observability.new_correlation()
            self.observability.emit(
                level=level,
                method=method,
                route=route,
                duration_ms=duration_ms,
                message=f"{event}: {message}".strip(),
                data_integrity=data_integrity,
                extra=extra or {},
            )
        except Exception as exc:
            logger.debug("Observabilidade dos pilares indisponível: %s", exc)

    def record_telemetry(self, snapshot: Dict[str, Any]) -> None:
        """Enfileira snapshot no hot path e deixa o callback gravar no DB canônico."""
        match_id = str(snapshot.get("match_id") or snapshot.get("fixtureId") or snapshot.get("fixture_id") or "unknown")
        odds = float(snapshot.get("asian_corner_odds") or snapshot.get("odds") or 0.0)
        line = float(snapshot.get("asian_corner_line") or snapshot.get("line") or 0.0)
        velocity = float(snapshot.get("odds_velocity") or 0.0)
        corners = snapshot.get("corners") if isinstance(snapshot.get("corners"), dict) else {}
        corner_count = corners.get("total")
        if corner_count is None:
            corner_count = float(corners.get("home") or 0.0) + float(corners.get("away") or 0.0)
        self.hybrid_store.write(
            match_id=match_id,
            odds=odds,
            odds_velocity=velocity,
            asian_line=line,
            corner_count=int(corner_count or 0),
            market_type="asian_corner",
            extra={"decision": snapshot.get("decision"), "source": snapshot.get("source")},
        )

    def evaluate_pillar5(self, snapshot: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Executa o Pilar 5 como gate observacional adicional sem abrir ordens."""
        try:
            odds = float(snapshot.get("asian_corner_odds") or 0.0)
            expected = snapshot.get("expected_corners")
            if expected is None:
                expected = float(snapshot.get("lam_home") or 0.0) + float(snapshot.get("lam_away") or 0.0)
            line = float(snapshot.get("asian_corner_line") or 9.5)
            velocity = float(analysis.get("odds_velocity") or snapshot.get("odds_velocity") or 0.0)
            result: RiskDecision = self.risk.evaluate(
                odds=odds,
                expected_corners=max(0.1, float(expected)),
                line=line,
                odds_velocity=velocity,
                bankroll=1000.0,
                wom_red_flag=velocity > 1.5,
            )
            return {
                "approved": bool(result.approved),
                "risk_reason_code": result.risk_reason_code,
                "edge": result.edge,
                "kelly_fraction": result.kelly_fraction,
                "recommended_stake": result.recommended_stake,
                "poisson_prob": result.poisson_prob,
                "gates": result.gates,
                "paper_trade": True,
            }
        except Exception as exc:
            logger.exception("Pilar 5 falhou no modo observacional: %s", exc)
            return {"approved": False, "risk_reason_code": "P5_UNAVAILABLE", "error": str(exc), "paper_trade": True}

    def online_feedback(self, team_key: str, confidence: float, is_correct: bool) -> Dict[str, Any]:
        confidence = max(0.0, min(1.0, float(confidence)))
        result = self.alpha.feedback(str(team_key or "unknown"), confidence, bool(is_correct))
        self.log_event(
            "online_feedback",
            method="POST",
            route="/api/feedback",
            message=str(team_key or "unknown"),
            extra={"confidence": confidence, "is_correct": bool(is_correct), "new_weight": result.get("new_weight")},
        )
        return result

    def status(self) -> Dict[str, Any]:
        return {
            "hybrid_store": self.hybrid_store.stats(),
            "router": self.router.stats(),
            "online_alpha": self.alpha.stats(),
            "event_log_dir": EVENT_LOG_DIR,
            "canonical_db": CANONICAL_DB,
            "paper_trade": True,
        }

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.hybrid_store.shutdown()
        except Exception as exc:
            logger.warning("Falha no shutdown do store híbrido: %s", exc)
        try:
            self.observability.shutdown()
        except Exception as exc:
            logger.warning("Falha no shutdown da observabilidade: %s", exc)


_runtime: Optional[PillarRuntime] = None
_runtime_lock = threading.Lock()


def get_pillar_runtime() -> PillarRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None or _runtime._closed:
            _runtime = PillarRuntime()
        return _runtime

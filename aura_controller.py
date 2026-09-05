"""Fachada advisory do AURA IA One → Hermes.

O controller não executa ordens. Ele orquestra a análise, reaplica o Paper
Lock e grava somente metadados redigidos no AuditLedger canônico quando um
ledger está disponível.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from engine.agents.aura_hermes_router import route_corner_analysis
from engine.aura_ai_one.adapter import AuraAIOneAdapter, HermesAuditAdapter
from engine.aura_ai_one.contracts import AuraHermesEnvelope


class AuraController:
    """Orquestrador local de análise de escanteios, sem autoridade de execução."""

    def __init__(
        self,
        *,
        ledger: Any | None = None,
        aura_adapter: AuraAIOneAdapter | None = None,
        hermes_adapter: HermesAuditAdapter | None = None,
        actor: str = "aura-controller",
    ) -> None:
        self.ledger = ledger
        self.aura_adapter = aura_adapter
        self.hermes_adapter = hermes_adapter
        self.actor = actor

    def _canonical_ledger(self) -> Any:
        if self.ledger is not None:
            return self.ledger
        from scripts.aura_admin_core import AuditLedger

        return AuditLedger()

    def evaluate_corners(
        self,
        points: Iterable[Mapping[str, Any]],
        *,
        fixture_id: str | None = None,
    ) -> AuraHermesEnvelope:
        envelope = route_corner_analysis(
            points,
            fixture_id=fixture_id,
            aura_adapter=self.aura_adapter,
            hermes_adapter=self.hermes_adapter,
        )
        ledger = self._canonical_ledger()
        payload = {
            "contract_version": envelope.contract_version,
            "fixture_id": envelope.fixture_id,
            "correlation_id": envelope.correlation_id,
            "final_decision": envelope.final_decision,
            "final_confidence": envelope.final_confidence,
            "review_status": envelope.review.status,
            "paper_trade": True,
            "execution_allowed": False,
            "approved": False,
            "stake_pct": 0.0,
            "exposure": 0.0,
        }
        try:
            ledger.append_event(
                trace_id=envelope.correlation_id,
                task_id=envelope.fixture_id,
                event_type="aura.corners.advisory_evaluation",
                actor=self.actor,
                status=envelope.final_decision,
                payload=payload,
                idempotency_key=f"aura-corners:{envelope.correlation_id}",
            )
        except Exception as exc:
            raise RuntimeError("AUDIT_LEDGER_UNAVAILABLE") from exc
        return envelope


__all__ = ["AuraController"]

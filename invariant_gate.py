#!/usr/bin/env python3
"""
invariant_gate.py — O módulo mais crítico do sistema.
Garante que NENHUMA decisão chega ao execution_router sem:
- paper_trade = True
- execution_allowed = False
- advisory_only = True
Localização: engine/invariant_gate.py
Aplicação: engine/agent_registry.py (dispatcher)
Verificação final: engine/execution_router.py
CAMADAS DE DEFESA:
1. InvariantGate.enforce() — conversão raw → GatedDecision no dispatcher
2. GatedDecision validators — rejeitam construção inválida
3. execution_router isinstance check — rejeita non-GatedDecision
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
logger = logging.getLogger(__name__)
class GatedDecision(BaseModel):
    """
    Tipo imutável e validado. Não pode existir com invariantes violados.
    Propriedades garantidas pela construção:
    - paper_trade: True (sempre, imutável)
    - execution_allowed: False (sempre, imutável)
    - advisory_only: True (sempre, imutável)
    - frozen=True: campos não podem ser alterados após construção
    - extra="forbid": campos inesperados são rejeitados
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    decision: Literal["ENTRA", "AGUARDA", "NAO_ENTRA"]
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    paper_trade: bool = True
    execution_allowed: bool = False
    advisory_only: bool = True
    agent_name: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    mode: Literal["neural", "fallback_glm"] = "neural"
    warnings: List[str] = Field(default_factory=list)
    blocked_reason: Optional[str] = None
    @field_validator("paper_trade")
    @classmethod
    def enforce_paper_trade(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("INVARIANT: paper_trade must be True")
        return True
    @field_validator("execution_allowed")
    @classmethod
    def enforce_execution_allowed(cls, v: bool) -> bool:
        if v is not False:
            raise ValueError("INVARIANT: execution_allowed must be False")
        return False
    @field_validator("advisory_only")
    @classmethod
    def enforce_advisory_only(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("INVARIANT: advisory_only must be True")
        return True
class InvariantGate:
    """
    Gate que converte output de qualquer agente em GatedDecision.
    Política:
    - paper_trade is False ou execution_allowed is True → BLOCKED (AGUARDA + log)
    - advisory_only=False ou campos em falta → WARNING (força + prossegue)
    - Tudo OK → pass
    """
    BLOCK_LOG = Path("data/invariant_blocks.jsonl")
    @staticmethod
    def enforce(
        agent_name: str,
        raw: Dict[str, Any],
        mode: str = "neural",
    ) -> GatedDecision:
        """
        Converte raw decision em GatedDecision segura.
        Args:
            agent_name: nome do agente que produziu a decisão
            raw: dict cru do agente
            mode: "neural" ou "fallback_glm"
        Returns:
            GatedDecision com invariantes garantidos
        """
        InvariantGate.BLOCK_LOG.parent.mkdir(parents=True, exist_ok=True)
        warnings: List[str] = []
        blocked_reason: Optional[str] = None
        paper_trade = raw.get("paper_trade", True)
        execution_allowed = raw.get("execution_allowed", False)
        advisory_only = raw.get("advisory_only", True)
        # Detectar campos em falta
        if "paper_trade" not in raw:
            warnings.append("paper_trade missing — defaulted to True")
        if "execution_allowed" not in raw:
            warnings.append("execution_allowed missing — defaulted to False")
        if "advisory_only" not in raw:
            warnings.append("advisory_only missing — defaulted to True")
        # BLOCKED — invariantes existenciais
        if paper_trade is False or execution_allowed is True:
            blocked_reason = (
                f"invariant_blocked: paper_trade={paper_trade}, "
                f"execution_allowed={execution_allowed}"
            )
            InvariantGate._log_block(agent_name, raw, blocked_reason)
            return GatedDecision(
                decision="AGUARDA",
                confidence=0.0,
                agent_name=agent_name,
                mode=mode,
                warnings=warnings,
                blocked_reason=blocked_reason,
            )
        # WARNING — advisory_only é hygiene
        if advisory_only is False:
            warnings.append("advisory_only was False — forced to True")
            advisory_only = True
        # Construção normal
        decision_str = raw.get("decision", "AGUARDA")
        confidence_val = float(raw.get("confidence", 0.0))
        confidence_val = max(0.0, min(1.0, confidence_val))
        return GatedDecision(
            decision=decision_str,
            confidence=confidence_val,
            paper_trade=True,
            execution_allowed=False,
            advisory_only=True,
            agent_name=agent_name,
            mode=mode,
            warnings=warnings,
            blocked_reason=None,
        )
    @staticmethod
    def _log_block(
        agent_name: str,
        raw: Dict[str, Any],
        reason: str,
    ) -> None:
        """Log forense de bloqueio — append-only JSONL."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent_name,
            "reason": reason,
            "raw_decision": raw,
        }
        try:
            with open(InvariantGate.BLOCK_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Falha ao escrever block log: %s", e)
        logger.warning(
            "INVARIANT BLOCKED: agent=%s reason=%s raw_decision=%s",
            agent_name,
            reason,
            json.dumps(raw, ensure_ascii=False)[:200],
        )

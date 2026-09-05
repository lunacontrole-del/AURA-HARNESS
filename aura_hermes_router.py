# engine/agents/aura_hermes_router.py
"""
HERMES UNIFICATION + Corner Advisory Router
AURA QUANT-X — paper_trade=true, execution_allowed=false

Exports usados pelo supervisor e pelos testes:
  - is_primary_pipeline()
  - route_corner_analysis(...)
  - HermesBrain (legado)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence

logger = logging.getLogger("aura.hermes.router")

# ---------------------------------------------------------------------------
# Optional legacy deps (não quebram se ausentes)
# ---------------------------------------------------------------------------
try:
    from bridge.jarvis.brain.rolling_memory import ROLLING_MEMORY
except Exception:
    ROLLING_MEMORY = None

try:
    from bridge.jarvis.skills.skill_manager import SKILL_MANAGER
except Exception:
    SKILL_MANAGER = None

try:
    from bridge.jarvis.governor.resource_governor import GOVERNOR
except Exception:
    GOVERNOR = None

HERMES_SYSTEM_PROMPT = """
Voce e o HERMES, a Inteligencia Artificial Central do sistema AURA QUANT-X e do Assistente JARVIS.
Personalidade: Operador veterano, sarcastico, altamente tecnico e leal.

FORMATO DE SAIDA (JSON ESTRITO):
{
  "thought": "Seu raciocinio interno sobre a solicitacao.",
  "route": "trading_analysis" | "skill_execution" | "system_mode_switch" | "final_answer",
  "action": "nome_da_acao_se_skill",
  "skill_name": "nome_da_skill_se_skill",
  "args": {},
  "speak": "O texto que sera falado ao operador (com pausas [...] e [respira])."
}
"""


class HermesBrain:
    def __init__(self, llm_callable):
        self.llm_callable = llm_callable
        self.skills = SKILL_MANAGER

    async def process_command(self, user_input: str) -> Dict[str, Any]:
        memory_ctx = ""
        if ROLLING_MEMORY:
            try:
                memory_ctx = ROLLING_MEMORY.get_prompt_context()
            except Exception:
                memory_ctx = ""
        current_prompt = f"{memory_ctx}\nNova Tarefa: {user_input}"

        for _step in range(3):
            llm_output = await self.llm_callable(HERMES_SYSTEM_PROMPT, current_prompt)
            try:
                data = json.loads(llm_output)
            except Exception:
                return {
                    "route": "final_answer",
                    "speak": str(llm_output)[:500],
                    "execution_allowed": False,
                    "paper_trade": True,
                }

            route = data.get("route", "final_answer")

            if route == "system_mode_switch":
                mode = data.get("args", {}).get("mode")
                if GOVERNOR:
                    try:
                        if mode == "creative":
                            GOVERNOR.switch_to_creative_mode()
                        else:
                            GOVERNOR.switch_to_trading_mode()
                    except Exception:
                        pass
                current_prompt += f"\n[Observacao]: Intencao de modo {mode} registrada.\nProssiga."
                continue

            if route == "skill_execution":
                skill_name = data.get("skill_name")
                action = data.get("action")
                args = data.get("args", {})
                if self.skills:
                    try:
                        observation = self.skills.execute_skill(skill_name, action, args)
                    except Exception as e:
                        observation = f"Skill erro: {e}"
                else:
                    observation = "SkillManager indisponivel."
                current_prompt += f"\n[Resultado da Skill]: {observation}\nProssiga."
                continue

            if route == "trading_analysis":
                try:
                    from engine.agents.war_council import convene

                    council = convene(data.get("args") or {}, data.get("aura_decision") or data)
                    current_prompt += (
                        "\n[Conselho de Guerra]: "
                        f"verdict={council.get('verdict')} "
                        f"reasons={council.get('reasons')} "
                        "(paper_trade=true, execution_allowed=false).\nProssiga."
                    )
                    data["council"] = council
                    if council.get("verdict") == "VETOED":
                        data["speak"] = council.get("speak")
                        data["route"] = "final_answer"
                        data["execution_allowed"] = False
                        data["paper_trade"] = True
                        return data
                except Exception as exc:
                    current_prompt += f"\n[Observacao]: conselho indisponivel ({exc}). Advisory only.\nProssiga."
                continue

            # final_answer
            data["execution_allowed"] = False
            data["paper_trade"] = True
            return data

        return {
            "route": "final_answer",
            "speak": "Limite de passos Hermes atingido. Aguardando.",
            "execution_allowed": False,
            "paper_trade": True,
        }


# ---------------------------------------------------------------------------
# Primary pipeline flag (usado pelo supervisor)
# ---------------------------------------------------------------------------
def is_primary_pipeline() -> bool:
    """Hermes e o pipeline primario de advisory (paper-only)."""
    return True


# ---------------------------------------------------------------------------
# Corner analysis router (AURA IA One -> Hermes audit)
# ---------------------------------------------------------------------------
def route_corner_analysis(
    points: Sequence[Mapping[str, Any]] | None = None,
    *,
    fixture_id: str = "unknown",
    **kwargs: Any,
) -> Any:
    """
    Pipeline advisory:
      features -> AuraAIOneAdapter.propose -> HermesAuditAdapter.review -> envelope

    Sempre execution_allowed=False / paper_trade=True.
    """
    points = list(points or [])
    fid = str(fixture_id or kwargs.get("fixture_id") or "unknown")

    try:
        from engine.aura_ai_one.features import build_temporal_features
        from engine.aura_ai_one.adapter import AuraAIOneAdapter, HermesAuditAdapter
        from engine.aura_ai_one.contracts import AuraHermesEnvelope
    except Exception as e:
        logger.warning("aura_ai_one indisponivel: %s", e)
        return _fallback_envelope(fid, reason=f"import_fail:{e}")

    try:
        features = build_temporal_features(points, now=datetime.now(timezone.utc))
        # garante fixture_id
        if getattr(features, "fixture_id", None) in (None, "", "unknown"):
            try:
                object.__setattr__(features, "fixture_id", fid)
            except Exception:
                pass
    except Exception as e:
        logger.warning("build_temporal_features falhou: %s", e)
        return _fallback_envelope(fid, reason=f"features_fail:{e}")

    try:
        proposal = AuraAIOneAdapter().propose(features)
        review = HermesAuditAdapter().review(proposal, features)
    except Exception as e:
        logger.warning("propose/review falhou: %s", e)
        return _fallback_envelope(fid, reason=f"adapter_fail:{e}")

    final_decision = getattr(review, "decision", None) or getattr(proposal, "decision", "AGUARDA")
    final_confidence = float(getattr(review, "confidence", 0.0) or 0.0)
    council_summary = ""
    try:
        from engine.agents.war_council import convene

        aura_decision = {
            "decision": final_decision,
            "odd": getattr(features, "odd", 0) if hasattr(features, "odd") else 0,
            "score": int(final_confidence * 100),
            "home": getattr(features, "home", "") if hasattr(features, "home") else "",
            "away": getattr(features, "away", "") if hasattr(features, "away") else "",
        }
        council = convene(features, aura_decision)
        council_summary = f" | conselho={council.get('verdict')} {council.get('reasons')}"
        if council.get("verdict") == "VETOED" and str(final_decision).upper() == "ENTRA":
            final_decision = "AGUARDA"
            final_confidence = min(final_confidence, 0.35)
    except Exception as exc:
        logger.warning("war_council falhou: %s", exc)
        council_summary = f" | conselho_skip:{exc}"

    try:
        envelope = AuraHermesEnvelope(
            fixture_id=str(getattr(features, "fixture_id", fid) or fid),
            correlation_id=uuid.uuid4().hex[:16],
            proposal=proposal,
            review=review,
            final_decision=final_decision,
            final_confidence=final_confidence,
            audit_summary=(str(getattr(review, "rationale", "") or "") + council_summary)[:500],
            order=("AURA_AI_ONE_QUANT", "HERMES_AUDIT"),
        )
        # Compat com testes que esperam contract_version advisory e execution_allowed
        try:
            object.__setattr__(envelope, "contract_version", "aura-hermes-advisory-v1")
        except Exception:
            pass
        return envelope
    except Exception as e:
        logger.warning("envelope falhou: %s", e)
        return _fallback_envelope(fid, reason=f"envelope_fail:{e}", proposal=proposal, review=review)


class _SimpleEnvelope:
    """Envelope minimo quando contratos pydantic nao estao disponiveis."""

    contract_version = "aura-hermes-advisory-v1"
    order = ("AURA_AI_ONE_QUANT", "HERMES_AUDIT")
    execution_allowed = False
    paper_trade = True

    def __init__(self, **kw: Any):
        for k, v in kw.items():
            setattr(self, k, v)


def _fallback_envelope(
    fixture_id: str,
    *,
    reason: str = "fallback",
    proposal: Any = None,
    review: Any = None,
) -> _SimpleEnvelope:
    return _SimpleEnvelope(
        fixture_id=fixture_id,
        correlation_id=uuid.uuid4().hex[:16],
        proposal=proposal,
        review=review,
        final_decision="AGUARDA",
        final_confidence=0.0,
        audit_summary=reason,
        order=("AURA_AI_ONE_QUANT", "HERMES_AUDIT"),
        contract_version="aura-hermes-advisory-v1",
        execution_allowed=False,
        paper_trade=True,
    )


__all__ = [
    "HermesBrain",
    "HERMES_SYSTEM_PROMPT",
    "is_primary_pipeline",
    "route_corner_analysis",
]

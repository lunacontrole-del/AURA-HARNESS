"""P2 Fase 6 — ExplanationCard determinístico.

O LLM só recebe este cartão. Nunca calcula nem altera a decisão.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_explanation_card(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Cartão factual estável para narrador / voz / Telegram."""
    integrity = analysis.get("data_integrity") or {}
    risk = analysis.get("risk") or {}
    gates = analysis.get("risk_gates") or {}
    fq = analysis.get("feature_quality") or {}

    action = str(analysis.get("decision") or analysis.get("signal") or "HOLD")
    facts: List[str] = []
    uncertainties: List[str] = []
    reason_codes: List[str] = []

    # Facts from analytics / pressure
    analytics = analysis.get("analytics") or {}
    if analytics.get("intensidade") is not None:
        try:
            facts.append(f"intensidade={float(analytics['intensidade']):.2f}")
        except (TypeError, ValueError):
            pass
    if analytics.get("velocidadeAtaques_dDA_dt") is not None:
        try:
            facts.append(f"dDA/dt={float(analytics['velocidadeAtaques_dDA_dt']):.2f}")
        except (TypeError, ValueError):
            pass

    issues = list(integrity.get("issues") or [])
    warnings = list(integrity.get("warnings") or [])
    reason_codes.extend(issues)
    reason_codes.extend(warnings)
    reason_codes.extend(gates.get("failed_gates") or [])
    reason_codes.extend(analysis.get("skill_kills") or [])

    if issues:
        uncertainties.append("integridade com issues")
    if warnings:
        uncertainties.append("avisos de captura")
    if analysis.get("price") is None and action.startswith("BUY"):
        uncertainties.append("odds indisponíveis")
    if (gates.get("cooldown") or {}).get("active"):
        uncertainties.append("cooldown ativo")
        reason_codes.append("COOLDOWN")

    # Humor policy
    humor_allowed = _humor_allowed(action, reason_codes, analysis)

    # Voice profile
    voice_profile = _voice_profile(action, reason_codes)

    p_cal = analysis.get("corner_prob")
    try:
        p_cal_f = float(p_cal) if p_cal is not None else None
    except (TypeError, ValueError):
        p_cal_f = None

    dqs = None
    if fq.get("status_counts"):
        ok = int((fq.get("status_counts") or {}).get("OK") or 0)
        total = sum(int(v) for v in (fq.get("status_counts") or {}).values()) or 1
        dqs = int(round(100.0 * ok / total))

    ci = analysis.get("corner_intelligence") or {}
    for f in (ci.get("facts") or [])[:6]:
        if str(f) not in facts:
            facts.append(str(f))
    if ci.get("pace", {}).get("regime"):
        regime = ci["pace"]["regime"]
        if f"regime={regime}" not in facts:
            facts.append(f"regime={regime}")
    if p_cal_f is None and ci.get("primary_probability") is not None:
        try:
            p_cal_f = float(ci["primary_probability"])
        except (TypeError, ValueError):
            pass

    card = {
        "fixture_id": str(analysis.get("fixtureId") or ""),
        "match_clock": str(analysis.get("clock") or analysis.get("minute") or ""),
        "score": _score_str(analysis),
        "action": action,
        "horizon": "5m",
        "p_calibrated": p_cal_f,
        "uncertainty": analysis.get("uncertainty"),
        "data_quality": dqs,
        "facts": facts[:8],
        "uncertainties": uncertainties[:6],
        "reason_codes": list(dict.fromkeys(str(c) for c in reason_codes))[:12],
        "humor_allowed": humor_allowed,
        "voice_profile": voice_profile,
        "decision_id": analysis.get("decision_id"),
        "event_id": analysis.get("event_id"),
        "risk_approved": bool(risk.get("approved")),
        "kelly": 0.0,  # always narrated as zero while disabled
        "policy": "p2_explanation_card_v1",
        "corner_regime": (ci.get("pace") or {}).get("regime") if isinstance(ci, dict) else None,
        "corner_windows": ci.get("windows") if isinstance(ci, dict) else None,
        "analyst_product": "corner_analysis_central",
    }
    return card


def _score_str(analysis: Dict[str, Any]) -> str:
    score = analysis.get("score")
    if isinstance(score, str) and score:
        return score
    if isinstance(score, dict):
        h = score.get("home")
        a = score.get("away")
        if h is not None and a is not None:
            return f"{h}-{a}"
    return ""


def _humor_allowed(action: str, reason_codes: List[Any], analysis: Dict[str, Any]) -> bool:
    blocked_codes = {
        "BLOCKED_BY_DATA", "BLOCKED_BY_LEDGER", "BLOCKED_BY_RISK", "BLOCKED_BY_MARKET",
        "BLOCKED_BY_MODEL", "COOLDOWN", "GOAL", "RED_CARD", "CAPTURE_LOSS",
        "capture_stale_over_45s", "critical_features_missing",
    }
    if action.startswith("BLOCK"):
        return False
    for c in reason_codes:
        if str(c) in blocked_codes or "GOAL" in str(c).upper() or "RED" in str(c).upper():
            return False
    try:
        u = float(analysis.get("uncertainty") or 0)
        if u > 0.55:
            return False
    except (TypeError, ValueError):
        pass
    return True


def _voice_profile(action: str, reason_codes: List[Any]) -> str:
    if action.startswith("BLOCK") or any("RISK" in str(c) for c in reason_codes):
        return "risk_guard"
    if action.startswith("BUY"):
        return "alert_focus"
    if action.startswith("WATCH"):
        return "watch_calm"
    return "neutral"


def card_to_narrator_prompt(card: Dict[str, Any]) -> str:
    """Prompt restrito: narrar somente fatos do cartão, sem alterar action."""
    return (
        "Você é o narrador da AURA QUANT-X. "
        "Fale em português do Brasil, claro e objetivo. "
        "Use APENAS os fatos do ExplanationCard abaixo. "
        "NÃO invente probabilidade, NÃO suavize bloqueios, NÃO mude a action. "
        "Se action começa com BLOCKED, diga que está bloqueado e o motivo (reason_codes). "
        "Se humor_allowed for false, tom sério, sem piada.\n\n"
        f"ExplanationCard:\n{card}"
    )

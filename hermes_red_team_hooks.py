# engine/agents/hermes_red_team_hooks.py
"""
Hooks leves para integrar Red Team + ROI no Hermes (advisory only).
Nao substitui o aura_hermes_router completo - use sob supervisao.
"""
from typing import Dict, Any

try:
    from engine.agents_glm.red_team_adversary import RED_TEAM
except Exception:
    RED_TEAM = None

try:
    from engine.agents.dynamic_thresholds import ONLINE_TUNER
except Exception:
    ONLINE_TUNER = None

try:
    from engine.agents_glm.roi_auditor_agent import ROI_AUDITOR
except Exception:
    ROI_AUDITOR = None


def audit_trade_decision(features: dict, aura_decision: dict) -> Dict[str, Any]:
    if RED_TEAM is None:
        return {"verdict": "SKIP", "reasons": ["RED_TEAM indisponivel"]}
    return RED_TEAM.audit_decision(features, aura_decision)


def format_roi_report(days: int = 1) -> str:
    if ROI_AUDITOR is None:
        return "ROI_AUDITOR indisponivel."
    stats = ROI_AUDITOR.get_daily_stats(days=days)
    if "error" in stats:
        return f"Erro: {stats['error']}."
    return (
        f"RELATORIO DE PERFORMANCE AURA INTEL ({days}d)\n\n"
        f"Tips: {stats['total_tips']}\n"
        f"Wins: {stats['wins']} | Losses: {stats['losses']} | Voids: {stats['voids']}\n\n"
        f"Hit Rate: {stats['hit_rate']:.1f}%\n"
        f"ROI Geral: {stats['roi']:.1f}%\n\n"
        f"Melhor Mercado: {stats['best_market']}\n"
    )

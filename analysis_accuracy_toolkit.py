"""Toolkit de acertividade + interatividade analítica (AURA 12.7.18).

Produz scores auxiliares e perguntas proativas SEM alterar a decisão de risco
(paper / approved=false permanece soberano). Não inventa dados ausentes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def build_accuracy_pack(analysis: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pacote de acertividade anexado à análise."""
    payload = payload or {}
    integrity = analysis.get("data_integrity") or {}
    veracity = _num(integrity.get("veracity_score"))
    status = str(integrity.get("status") or "").upper()

    p = _num(
        analysis.get("calibrated_probability")
        or analysis.get("p_calibrated")
        or analysis.get("corner_prob")
        or analysis.get("probability")
    )
    edge = _num(analysis.get("edge") or analysis.get("ev"))
    market_p = _num(analysis.get("market_prob") or analysis.get("implied_prob"))

    features = analysis.get("features") or {}
    pressure = _num(features.get("pressure_slope") or analysis.get("pressure_slope"))
    odds_vel = _num(features.get("odds_velocity") or analysis.get("odds_velocity"))

    # --- Confluência de sinais (0-100) ---
    signals: List[Dict[str, Any]] = []
    score = 50.0

    if veracity is not None:
        signals.append({"name": "veracidade", "value": veracity, "weight": 0.25})
        score = score * 0.5 + veracity * 0.5
    if status == "BLOCK":
        score = min(score, 25.0)
        signals.append({"name": "integridade", "value": 0, "note": "BLOCK"})
    elif status == "WARN":
        score -= 8
        signals.append({"name": "integridade", "value": 60, "note": "WARN"})
    else:
        signals.append({"name": "integridade", "value": 90, "note": "OK"})

    if p is not None:
        # probabilidade isolada não é edge; contribui pouco
        signals.append({"name": "prob_modelo", "value": _clip(p * 100 if p <= 1 else p), "weight": 0.15})
        score += 4 if (p if p > 1 else p * 100) >= 55 else -2

    if edge is not None:
        e = edge if abs(edge) > 1 else edge * 100
        signals.append({"name": "edge", "value": e, "weight": 0.2})
        if e >= 3:
            score += 10
        elif e >= 1:
            score += 4
        elif e < 0:
            score -= 8

    if market_p is not None and p is not None:
        mp = market_p if market_p > 1 else market_p * 100
        pp = p if p > 1 else p * 100
        gap = pp - mp
        signals.append({"name": "modelo_vs_mercado", "value": gap, "weight": 0.15})
        if gap >= 4:
            score += 8
        elif gap <= -4:
            score -= 6

    if odds_vel is not None:
        signals.append({"name": "odds_velocity", "value": odds_vel, "weight": 0.1})
        # política AURA: velocity alta positiva = anti-red / cautela
        if odds_vel > 1.5:
            score -= 12
        elif odds_vel <= -5:
            score += 6

    if pressure is not None:
        signals.append({"name": "pressure_slope", "value": pressure, "weight": 0.1})
        if pressure > 0:
            score += 4
        elif pressure < 0:
            score -= 3

    accuracy = round(_clip(score), 1)

    # --- Nível de confiança operacional ---
    if status == "BLOCK" or (veracity is not None and veracity < 40):
        confidence = "BAIXA"
        posture = "NÃO OPERAR — dados ou integridade insuficientes"
    elif accuracy >= 72 and status == "OK":
        confidence = "ALTA"
        posture = "Sinais alinhados — ainda em paper / observação de risco"
    elif accuracy >= 55:
        confidence = "MÉDIA"
        posture = "Observar — confluência parcial"
    else:
        confidence = "BAIXA"
        posture = "Aguardar melhor quadro de dados/mercado"

    # --- Perguntas proativas para o usuário (interatividade) ---
    questions: List[str] = []
    if status != "OK":
        questions.append("Os dados da captura estão estáveis ou ainda há atraso/conflito de fonte?")
    if edge is None and p is not None:
        questions.append("Quer que eu compare a probabilidade do modelo com a odd implícita do mercado de escanteios?")
    if odds_vel is not None and odds_vel > 1.0:
        questions.append("A odd está subindo rápido — prefere que eu trate isso como veto de mercado?")
    if pressure is not None and pressure > 0 and (edge is None or (edge is not None and edge < 0.02)):
        questions.append("Há pressão de campo, mas pouco edge — quer o cenário só de observação para os próximos 5 minutos?")
    if not questions:
        questions.append("Quer o foco nos próximos 5 minutos de escanteio ou no quadro completo de risco?")

    # --- Checklist de acertividade ---
    checklist = [
        {"item": "Integridade/veracidade", "ok": status != "BLOCK"},
        {"item": "Probabilidade do modelo presente", "ok": p is not None},
        {"item": "Mercado/odd implícita presente", "ok": market_p is not None},
        {"item": "Edge calculado", "ok": edge is not None},
        {"item": "Odds velocity observada", "ok": odds_vel is not None},
        {"item": "Pressão/momentum observado", "ok": pressure is not None},
    ]

    return {
        "schema": "aura-accuracy-pack-1",
        "accuracy_score": accuracy,
        "confidence": confidence,
        "score_type": "HEURISTIC_COMPLETENESS",
        "score_disclaimer": "Nao e acuracia preditiva historica. E score de confluencia/completude de dados. Nao usar como percentual de acerto.",
        "is_predictive_accuracy": False,
        "posture": posture,
        "signals": signals[:12],
        "checklist": checklist,
        "proactive_questions": questions[:4],
        "user_hints": [
            "Pergunte: 'e agora?', 'entra?', 'como está a odd?', 'pressionando?'",
            "Resumo rápido: diga 'resumo' ou use o botão na Central",
        ],
        "safe": status != "BLOCK",
    }


def attach_to_analysis(analysis: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    pack = build_accuracy_pack(analysis, payload)
    analysis = dict(analysis or {})
    analysis["accuracy_pack"] = pack
    # Espelha campos leves no topo para UI/voz
    analysis["accuracy_score"] = pack["accuracy_score"]
    analysis["analysis_confidence"] = pack["confidence"]
    return analysis

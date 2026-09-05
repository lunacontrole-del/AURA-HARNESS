"""Central de análise especializada em escanteios — inteligência analítica.

Não autoriza stake. Produz:
- ritmo e timing de escanteios
- pressão → lag até canto
- janelas de risco (próx. 1/3/5/10 min)
- comparativo casa/fora
- qualidade e lacunas do raciocínio
- cartão analítico para UI/narrador
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _f(x: Any, default: Optional[float] = None) -> Optional[float]:
    if x is None or x == "":
        return default
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _pair(stats: Dict[str, Any], *keys: str) -> Tuple[Optional[float], Optional[float]]:
    for k in keys:
        block = stats.get(k)
        if isinstance(block, dict):
            h = _f(block.get("home", block.get("h")))
            a = _f(block.get("away", block.get("a")))
            if h is not None or a is not None:
                return h, a
        if isinstance(block, (list, tuple)) and len(block) >= 2:
            return _f(block[0]), _f(block[1])
    return None, None


def _extract_corner_events(payload: Dict[str, Any], analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = payload.get("events") or payload.get("eventos") or analysis.get("matchEvents") or []
    out: List[Dict[str, Any]] = []
    if not isinstance(events, list):
        return out
    for ev in events:
        if isinstance(ev, dict):
            blob = " ".join(str(ev.get(k) or "") for k in ("type", "event", "name", "kind", "label")).lower()
            if "corner" not in blob and "escante" not in blob and "canto" not in blob:
                continue
            minute = _f(ev.get("minute", ev.get("min", ev.get("clock"))))
            side = str(ev.get("side") or ev.get("team") or "").lower()
            out.append({"minute": minute, "side": side, "raw": ev})
        else:
            blob = str(ev).lower()
            if "corner" in blob or "escante" in blob:
                out.append({"minute": None, "side": "", "raw": ev})
    return out


def _gaps(minutes: Sequence[Optional[float]]) -> List[float]:
    xs = sorted(m for m in minutes if m is not None)
    return [xs[i] - xs[i - 1] for i in range(1, len(xs)) if xs[i] is not None and xs[i - 1] is not None]


def analyze_corners(analysis: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Gera bloco corner_intelligence anexado à análise."""
    payload = payload if isinstance(payload, dict) else {}
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    if not stats and isinstance(analysis.get("stats"), dict):
        stats = analysis["stats"]

    minute = _f(payload.get("minute", analysis.get("minute")), 0.0) or 0.0
    c_h, c_a = _pair(stats, "corners", "escanteios")
    da_h, da_a = _pair(stats, "dangerous", "dangerousAttacks", "ataques_perigosos")
    att_h, att_a = _pair(stats, "attacks", "ataques")
    xg_h, xg_a = _pair(stats, "xg", "xG")
    shot_h, shot_a = _pair(stats, "shotsOn", "shots_on", "onTarget")

    c_h = c_h if c_h is not None else 0.0
    c_a = c_a if c_a is not None else 0.0
    corners_total = c_h + c_a
    da_h = da_h if da_h is not None else None
    da_a = da_a if da_a is not None else None

    corner_events = _extract_corner_events(payload, analysis)
    event_minutes = [e.get("minute") for e in corner_events]
    gaps = _gaps(event_minutes)
    avg_gap = sum(gaps) / len(gaps) if gaps else None
    last_corner_min = max((m for m in event_minutes if m is not None), default=None)
    minutes_since_last = (minute - last_corner_min) if last_corner_min is not None else None

    # pace
    pace_match = corners_total / max(1.0, minute) if minute > 0 else None
    # recent 5/10 min from events
    def count_since(window: float) -> int:
        if not event_minutes:
            # fallback: cannot know recent from totals only
            return -1  # unknown
        return sum(1 for m in event_minutes if m is not None and m >= minute - window)

    last5 = count_since(5)
    last10 = count_since(10)

    # pressure intensity
    da_total = None
    if da_h is not None or da_a is not None:
        da_total = (da_h or 0) + (da_a or 0)
    pressure_imbalance = None
    if da_total is not None and da_total > 0:
        pressure_imbalance = ((da_h or 0) - (da_a or 0)) / da_total

    # dangerous per corner (efficiency proxy)
    da_per_corner = (da_total / corners_total) if (da_total is not None and corners_total > 0) else None

    # Poisson-style remaining intensity from pace
    lambda_5 = None
    lambda_10 = None
    if pace_match is not None and pace_match >= 0:
        lambda_5 = pace_match * 5.0
        lambda_10 = pace_match * 10.0
    # boost if recent acceleration
    if last5 is not None and last5 >= 0 and last5 >= 2 and lambda_5 is not None:
        lambda_5 = max(lambda_5, last5 * 0.85)
    if last10 is not None and last10 >= 0 and last10 >= 3 and lambda_10 is not None:
        lambda_10 = max(lambda_10, last10 * 0.7)

    def p_at_least_one(lam: Optional[float]) -> Optional[float]:
        if lam is None:
            return None
        lam = max(0.0, float(lam))
        return 1.0 - math.exp(-lam)

    p_1m = p_at_least_one((lambda_5 / 5.0) if lambda_5 is not None else None)
    p_3m = p_at_least_one((lambda_5 * 0.6) if lambda_5 is not None else None)
    p_5m = p_at_least_one(lambda_5)
    p_10m = p_at_least_one(lambda_10)

    # prefer engine corner_prob when present for 5m horizon
    engine_p = _f(analysis.get("corner_prob"))
    if engine_p is not None:
        p_5m = engine_p

    # side pressure for next corner lean
    lean = "balanced"
    lean_score = 0.0
    if pressure_imbalance is not None:
        lean_score = pressure_imbalance
        if pressure_imbalance > 0.12:
            lean = "home"
        elif pressure_imbalance < -0.12:
            lean = "away"

    # analytical confidence (0-100) from data completeness
    completeness = 0
    checks = [
        minute > 0,
        c_h is not None or c_a is not None,
        da_h is not None or da_a is not None,
        len(corner_events) > 0,
        engine_p is not None,
        xg_h is not None or xg_a is not None,
    ]
    completeness = int(round(100.0 * sum(1 for c in checks if c) / len(checks)))

    gaps_note = []
    if not corner_events:
        gaps_note.append("timeline_de_escanteios_ausente")
    if da_total is None:
        gaps_note.append("ataques_perigosos_ausentes")
    if engine_p is None:
        gaps_note.append("probabilidade_engine_ausente")
    if minutes_since_last is not None and minutes_since_last > 12:
        gaps_note.append("seca_prolongada_de_escanteios")

    # narrative facts (structured, no invention)
    facts: List[str] = []
    facts.append(f"escanteios={int(c_h)}-{int(c_a)} (total {int(corners_total)})")
    if pace_match is not None:
        facts.append(f"ritmo={pace_match:.2f} cantos/min")
    if last_corner_min is not None:
        facts.append(f"último_canto≈{last_corner_min:.0f}'")
    if minutes_since_last is not None:
        facts.append(f"há={minutes_since_last:.1f} min sem canto")
    if last5 >= 0:
        facts.append(f"últimos_5min={last5} cantos")
    if last10 >= 0:
        facts.append(f"últimos_10min={last10} cantos")
    if avg_gap is not None:
        facts.append(f"intervalo_médio={avg_gap:.1f} min")
    if pressure_imbalance is not None:
        facts.append(f"desequilíbrio_pressão={pressure_imbalance:+.2f}")
    if lean != "balanced":
        facts.append(f"inclinação={lean}")

    windows = {
        "1m": {"p": p_1m, "label": "próx. 1 min"},
        "3m": {"p": p_3m, "label": "próx. 3 min"},
        "5m": {"p": p_5m, "label": "próx. 5 min", "primary": True},
        "10m": {"p": p_10m, "label": "próx. 10 min"},
    }

    # Regime classification
    regime = "normal"
    if pace_match is not None:
        if pace_match >= 0.35:
            regime = "alta_frequencia"
        elif pace_match <= 0.12 and minute >= 20:
            regime = "baixa_frequencia"
    if minutes_since_last is not None and minutes_since_last >= 10 and minute >= 25:
        regime = "seca"
    if last5 >= 2:
        regime = "rajada"

    # Quality flags for analyst
    quality_flags = list(gaps_note)
    integrity = analysis.get("data_integrity") or {}
    if integrity.get("status") == "BLOCK":
        quality_flags.append("integridade_BLOCK")
    for issue in (integrity.get("issues") or [])[:5]:
        quality_flags.append(str(issue))

    # Hawkes for/against (corners conceded explícito)
    hawkes = None
    try:
        from hawkes_corners import build_hawkes_from_payload
        hawkes = build_hawkes_from_payload(payload, analysis=analysis)
        # prefer hawkes match p for windows if engine_p missing
        if hawkes and hawkes.get("windows"):
            for k, w in (hawkes.get("windows") or {}).items():
                if k in windows and windows[k].get("p") is None and w.get("match") is not None:
                    windows[k]["p"] = w["match"]
            if p_5m is None and hawkes.get("p_match_5m") is not None:
                p_5m = hawkes["p_match_5m"]
                windows["5m"]["p"] = p_5m
        if hawkes and hawkes.get("defensive_stress"):
            facts.append(
                "stress_def_casa=" + str((hawkes["defensive_stress"].get("home") or {}).get("level"))
            )
            facts.append(
                "stress_def_fora=" + str((hawkes["defensive_stress"].get("away") or {}).get("level"))
            )
    except Exception as _h_exc:
        hawkes = {"ok": False, "error": str(_h_exc)}

    card = {
        "product": "corner_analysis_central",
        "version": "ci_v1",
        "fixture_id": str(analysis.get("fixtureId") or payload.get("fixtureId") or ""),
        "minute": minute,
        "score": analysis.get("score") or payload.get("score"),
        "totals": {
            "home": c_h,
            "away": c_a,
            "match": corners_total,
        },
        "pace": {
            "corners_per_minute": pace_match,
            "avg_gap_minutes": avg_gap,
            "minutes_since_last": minutes_since_last,
            "last_corner_minute": last_corner_min,
            "last_5min_count": last5 if last5 >= 0 else None,
            "last_10min_count": last10 if last10 >= 0 else None,
            "regime": regime,
        },
        "pressure": {
            "dangerous_home": da_h,
            "dangerous_away": da_a,
            "dangerous_total": da_total,
            "imbalance": pressure_imbalance,
            "da_per_corner": da_per_corner,
            "lean": lean,
            "lean_score": lean_score,
        },
        "windows": windows,
        "primary_horizon": "5m",
        "primary_probability": p_5m,
        "engine_probability": engine_p,
        "completeness_score": completeness,
        "facts": facts,
        "quality_flags": quality_flags,
        "event_count_timeline": len(corner_events),
        "generated_at": time.time(),
        "disclaimer": "Análise de escanteios. Não é ordem de aposta. Kelly/execução desligados.",
        "hawkes": hawkes,
    }
    return card


def analyst_brief(card: Dict[str, Any]) -> str:
    """Resumo textual determinístico para UI/voz."""
    if not card:
        return "Sem dados de escanteios."
    bits = []
    t = card.get("totals") or {}
    bits.append(f"Escanteios {int(t.get('home') or 0)}-{int(t.get('away') or 0)}.")
    pace = card.get("pace") or {}
    if pace.get("regime"):
        bits.append(f"Regime: {pace['regime']}.")
    if pace.get("minutes_since_last") is not None:
        bits.append(f"Sem canto há {pace['minutes_since_last']:.1f} min.")
    if pace.get("last_5min_count") is not None:
        bits.append(f"Últimos 5 min: {pace['last_5min_count']} cantos.")
    p = card.get("primary_probability")
    if p is not None:
        bits.append(f"Probabilidade próximo canto ~5 min: {p * 100:.0f}%.")
    press = card.get("pressure") or {}
    if press.get("lean") and press["lean"] != "balanced":
        bits.append(f"Pressão inclinada para {press['lean']}.")
    flags = card.get("quality_flags") or []
    if flags:
        bits.append("Ressalvas: " + ", ".join(flags[:3]) + ".")
    bits.append("Modo análise — sem stake.")
    return " ".join(bits)

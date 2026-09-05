"""Edge vs mercado — somente quando o mercado é semanticamente next_corner.

Regras:
- over_9_5 / total da partida → INCOMPATÍVEL (nunca edge)
- odds devem ser timestampadas e frescas
- edge = p_calibrada - 1/odds
- sem odds → sem edge (não inventa 1.80)
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

NEXT_CORNER_ALIASES = (
    "next_corner",
    "corner_next",
    "nextcorner",
    "escanteio_proximo",
    "proximo_escanteio",
    "próximo_escanteio",
    "corners_live",
    "escanteios_ao_vivo",
    "corner_5min",
    "corner_5_min",
    "next_corner_within_300s",
    "next_corner_within_5m",
    "live_corner",
)

TOTAL_ALIASES = (
    "over_9_5",
    "over_9.5",
    "under_9_5",
    "under_9.5",
    "over_8_5",
    "over_10_5",
    "match_total",
    "total_corners",
    "asian_total",
    "asian_corner_line",
    "corners_ou",
    "corners_over",
    "corners_under",
)

MAX_ODDS_AGE_SEC = 30.0


def normalize_market_name(name: Any) -> str:
    s = str(name or "").strip().lower()
    s = s.replace(" ", "_").replace("-", "_")
    s = s.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("é", "e")
    return s


def classify_market(name: Any) -> str:
    """Returns: next_corner | total | unknown | empty."""
    n = normalize_market_name(name)
    if not n:
        return "empty"
    for a in TOTAL_ALIASES:
        if a in n:
            return "total"
    for a in NEXT_CORNER_ALIASES:
        if a in n:
            return "next_corner"
    # bare "corner" / "escanteio" without total cues → treat as next_corner-ish live
    if "corner" in n or "escante" in n:
        if "total" in n or "over" in n or "under" in n or "linha" in n:
            return "total"
        return "next_corner"
    return "unknown"


def parse_odds_ts(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        ts = float(raw)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return ts
    except (TypeError, ValueError):
        return None


def odds_age_sec(odds_ts: Any, *, now: Optional[float] = None) -> Optional[float]:
    ts = parse_odds_ts(odds_ts)
    if ts is None:
        return None
    now = now if now is not None else time.time()
    return max(0.0, now - ts)


def compute_edge(
    p_model: Optional[float],
    odds: Optional[float],
    *,
    market_name: Any = None,
    odds_ts: Any = None,
    max_age_sec: float = MAX_ODDS_AGE_SEC,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Retorno estruturado — nunca inventa odd.
    compatible=False → edge não deve liberar BUY.
    """
    mclass = classify_market(market_name)
    out: Dict[str, Any] = {
        "compatible": False,
        "market_class": mclass,
        "market_name": normalize_market_name(market_name),
        "odds": None,
        "implied_prob": None,
        "p_model": None,
        "edge": None,
        "odds_age_sec": None,
        "code": "INIT",
        "message": "",
    }

    if mclass == "total":
        out["code"] = "MARKET_INCOMPATIBLE_TOTAL"
        out["message"] = "Mercado de total (ex. over_9_5) não equivale a next_corner_within_horizon"
        return out

    if mclass == "unknown":
        out["code"] = "MARKET_UNKNOWN"
        out["message"] = "Mercado não classificado como next_corner"
        return out

    # empty market name: only allow edge if odds present (legacy live corner feed)
    try:
        p = float(p_model) if p_model is not None else None
    except (TypeError, ValueError):
        p = None
    if p is None or not (0.0 < p < 1.0):
        out["code"] = "NO_MODEL_PROB"
        out["message"] = "Probabilidade do modelo ausente ou inválida"
        return out
    out["p_model"] = p

    try:
        od = float(odds) if odds is not None else None
    except (TypeError, ValueError):
        od = None
    if od is None or od <= 1.0:
        out["code"] = "NO_ODDS"
        out["message"] = "Odds ausentes — edge não calculado (não inventa 1.80)"
        return out
    out["odds"] = od
    implied = 1.0 / od
    out["implied_prob"] = implied

    age = odds_age_sec(odds_ts, now=now)
    out["odds_age_sec"] = age
    if odds_ts is not None and age is not None and age > max_age_sec:
        out["code"] = "ODDS_STALE"
        out["message"] = f"Odds velhas ({age:.1f}s > {max_age_sec}s)"
        return out
    if odds_ts is None:
        # allow computation but flag degraded freshness
        out["code"] = "ODDS_TS_MISSING"
        out["message"] = "Odds sem timestamp — edge informativo, não operacional"
        edge = p - implied
        out["edge"] = edge
        out["compatible"] = False  # strict: require timestamp for operational edge
        return out

    edge = p - implied
    out["edge"] = edge
    out["compatible"] = mclass in ("next_corner", "empty")
    if mclass == "empty":
        out["compatible"] = True  # legacy path with odds only
        out["code"] = "OK_LEGACY_ODDS"
        out["message"] = "Edge com odds legadas (sem nome de mercado)"
    else:
        out["code"] = "OK"
        out["message"] = "Edge operacional next_corner"
    return out

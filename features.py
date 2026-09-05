# features.py — Features estáveis para modelo de cantos/gols
# P0 quality gate: read_numeric + provenance; legacy vector preserved.
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple, Union
import math

# Status codes for numeric field reads
STATUS_OK = "OK"
STATUS_MISSING = "MISSING"
STATUS_INVALID = "INVALID"


def read_numeric(value: Any, *, field_name: str = "") -> Dict[str, Any]:
    """
    Read a numeric value without semantic defaults.
    - Real 0 → OK, value=0.0
    - None / key absent (caller passes None) → MISSING, value=None
    - "", "—", non-numeric → INVALID, value=None
    Returns dict: status, value (float|None), field, legacy_default (float for compat)
    """
    if value is None:
        return {
            "status": STATUS_MISSING,
            "value": None,
            "field": field_name,
            "legacy_default": 0.0,
        }
    if isinstance(value, bool):
        # bool is subclass of int; treat as invalid for stats
        return {
            "status": STATUS_INVALID,
            "value": None,
            "field": field_name,
            "legacy_default": 0.0,
        }
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return {
                "status": STATUS_INVALID,
                "value": None,
                "field": field_name,
                "legacy_default": 0.0,
            }
        return {
            "status": STATUS_OK,
            "value": float(value),
            "field": field_name,
            "legacy_default": float(value),
        }
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text in ("—", "-", "–", "N/A", "n/a", "null", "None", "undefined"):
            return {
                "status": STATUS_MISSING if text == "" else STATUS_INVALID,
                "value": None,
                "field": field_name,
                "legacy_default": 0.0,
            }
        try:
            num = float(text.replace(",", "."))
            if not math.isfinite(num):
                raise ValueError("non-finite")
            return {
                "status": STATUS_OK,
                "value": num,
                "field": field_name,
                "legacy_default": num,
            }
        except (TypeError, ValueError):
            return {
                "status": STATUS_INVALID,
                "value": None,
                "field": field_name,
                "legacy_default": 0.0,
            }
    return {
        "status": STATUS_INVALID,
        "value": None,
        "field": field_name,
        "legacy_default": 0.0,
    }


def _as_side_dict(block: Any) -> Dict[str, Any]:
    """Accept {home,away}, [h,a], or numeric scalar (ignored)."""
    if isinstance(block, dict):
        return block
    if isinstance(block, (list, tuple)) and len(block) >= 2:
        return {"home": block[0], "away": block[1]}
    return {}


def _side_block(stats: Dict[str, Any], key: str, aliases: Tuple[str, ...] = ()) -> Dict[str, Any]:
    block = stats.get(key)
    converted = _as_side_dict(block)
    if converted:
        return converted
    for a in aliases:
        converted = _as_side_dict(stats.get(a))
        if converted:
            return converted
    return {}


def frame_from_stats(stats: Dict[str, Any], *, return_bundle: bool = False) -> Union[List[float], Dict[str, Any]]:
    """
    Vetor base: [da_h, da_a, xg_h, xg_a, g_h, g_a, c_h, c_a].
    Com return_bundle=True retorna legacy_vector + provenance + quality.
    Chamadores antigos continuam recebendo List[float].
    """
    if not isinstance(stats, dict):
        stats = {}

    d = _side_block(stats, "dangerous", ("dangerousAttacks", "ataquesPerigosos"))
    x = _side_block(stats, "xg", ("xG",))
    g = _side_block(stats, "goals", ("gols",))
    c = _side_block(stats, "corners", ("escanteios",))

    fields_spec = [
        ("dangerous.home", d.get("home") if "home" in d else None),
        ("dangerous.away", d.get("away") if "away" in d else None),
        ("xg.home", x.get("home") if "home" in x else None),
        ("xg.away", x.get("away") if "away" in x else None),
        ("goals.home", g.get("home") if "home" in g else None),
        ("goals.away", g.get("away") if "away" in g else None),
        ("corners.home", c.get("home") if "home" in c else None),
        ("corners.away", c.get("away") if "away" in c else None),
    ]

    provenance: Dict[str, Any] = {}
    quality_counts = {STATUS_OK: 0, STATUS_MISSING: 0, STATUS_INVALID: 0}
    missing_fields: List[str] = []
    invalid_fields: List[str] = []
    legacy_vector: List[float] = []
    legacy_default_count = 0

    for name, raw in fields_spec:
        # Key absent → None (MISSING); present with null → MISSING; etc.
        read = read_numeric(raw, field_name=name)
        provenance[name] = read
        quality_counts[read["status"]] = quality_counts.get(read["status"], 0) + 1
        if read["status"] == STATUS_MISSING:
            missing_fields.append(name)
            legacy_default_count += 1
        elif read["status"] == STATUS_INVALID:
            invalid_fields.append(name)
            legacy_default_count += 1
        legacy_vector.append(float(read["legacy_default"]))

    quality = {
        "status_counts": quality_counts,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
        "legacy_default_count": legacy_default_count,
        "schema_version": "p0_quality_v1",
    }

    if return_bundle:
        return {
            "legacy_vector": legacy_vector,
            "provenance": provenance,
            "quality": quality,
        }
    return legacy_vector


def engineer_sequence(history: List[List[float]], seq_len: int = 15) -> Dict[str, float]:
    """
    A partir do histórico de frames, calcula features agregadas.
    history[i] = [da_h, da_a, xg_h, xg_a, g_h, g_a, c_h, c_a]
    """
    if not history:
        return {k: 0.0 for k in (
            "da_total", "xg_total", "corners_total", "goals_total",
            "dda_dt", "dxg_dt", "dc_dt", "da_ratio_home", "minute_proxy",
            "pressure_imbalance", "corner_pace",
        )}

    h = history[-seq_len:]
    last = h[-1]
    first = h[0]
    n = max(1, len(h) - 1)

    da_h, da_a = last[0], last[1]
    xg_h, xg_a = last[2], last[3]
    g_h, g_a = last[4], last[5]
    c_h, c_a = last[6], last[7]

    dda_dt = ((da_h + da_a) - (first[0] + first[1])) / n
    dxg_dt = ((xg_h + xg_a) - (first[2] + first[3])) / n
    dc_dt = ((c_h + c_a) - (first[6] + first[7])) / n

    da_total = da_h + da_a
    xg_total = xg_h + xg_a
    corners_total = c_h + c_a
    goals_total = g_h + g_a
    da_ratio_home = da_h / da_total if da_total > 1e-6 else 0.5
    pressure_imbalance = (da_h - da_a) / (da_total + 1e-6)
    minute_proxy = float(len(history))
    corner_pace = corners_total / max(1.0, minute_proxy)

    return {
        "da_total": da_total,
        "xg_total": xg_total,
        "corners_total": corners_total,
        "goals_total": goals_total,
        "dda_dt": dda_dt,
        "dxg_dt": dxg_dt,
        "dc_dt": dc_dt,
        "da_ratio_home": da_ratio_home,
        "minute_proxy": minute_proxy,
        "pressure_imbalance": pressure_imbalance,
        "corner_pace": corner_pace,
    }


def poisson_corner_prob(lambda_remaining: float) -> float:
    """P(ao menos 1 canto) ~ 1 - e^{-λ}."""
    lam = max(0.0, lambda_remaining)
    return 1.0 - math.exp(-lam)


def baseline_signal(feats: Dict[str, float], thr_dda: float = 1.5, thr_pace: float = 0.15) -> str:
    """Baseline simples: pressão subindo + ritmo de cantos."""
    if feats.get("dda_dt", 0) >= thr_dda and feats.get("corner_pace", 0) >= thr_pace:
        return "BUY_CORNER"
    if feats.get("dxg_dt", 0) >= 0.08 and feats.get("xg_total", 0) >= 0.8:
        return "BUY_GOAL"
    return "HOLD"

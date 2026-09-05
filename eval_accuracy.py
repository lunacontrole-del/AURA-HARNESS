"""Avaliação de acurácia real a partir de labels + decisões persistidas.

Sem labels limpos no banco → reporta cobertura zero e não inventa métricas.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from metrics import brier_score, log_loss, calibration_bins
from data_store import list_labels, list_decision_logs, get_conn, init_schema, DB_PATH


def _finite(x: Any) -> Optional[float]:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def join_decisions_and_labels(
    fixture_id: str,
    *,
    horizon_sec: int = 300,
    path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Emparelha decision_logs com labels pelo window_start_ts mais próximo.
    Labels censurados (label NULL) são excluídos da métrica supervisionada.
    """
    path = path or DB_PATH
    labels = list_labels(fixture_id, horizon_sec=horizon_sec, path=path)
    decisions = list_decision_logs(fixture_id, path=path)
    labeled = [lb for lb in labels if lb.get("censored") in (0, "0", False, None) and lb.get("label") is not None]
    # fix: censored=1 must exclude
    labeled = [lb for lb in labels if int(lb.get("censored") or 0) == 0 and lb.get("label") is not None]

    pairs: List[Dict[str, Any]] = []
    if not labeled or not decisions:
        return pairs

    for dec in decisions:
        dts = _finite(dec.get("ts"))
        if dts is None:
            continue
        # nearest label by window_start_ts
        best = None
        best_dt = 1e18
        for lb in labeled:
            w = _finite(lb.get("window_start_ts"))
            if w is None:
                continue
            d = abs(w - dts)
            if d < best_dt:
                best_dt = d
                best = lb
        if best is None or best_dt > 120:  # max 2 min mismatch
            continue
        pairs.append({
            "fixture_id": fixture_id,
            "decision_id": dec.get("decision_id"),
            "signal": dec.get("signal"),
            "prob": _finite(dec.get("corner_prob")),
            "label": int(best["label"]),
            "horizon_sec": horizon_sec,
            "match_dt_sec": best_dt,
            "label_id": best.get("label_id"),
        })
    return pairs


def evaluate_pairs(pairs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    probs = [p["prob"] for p in pairs if p.get("prob") is not None]
    labels = [p["label"] for p in pairs if p.get("prob") is not None]
    if len(probs) != len(labels) or not probs:
        return {
            "n": 0,
            "n_pairs": len(pairs),
            "brier": None,
            "log_loss": None,
            "hit_rate_at_50": None,
            "reliability": [],
            "message": "Sem pares label↔probabilidade suficientes. Precisa de labels não censurados + decisões.",
        }
    preds = [1 if (p or 0) >= 0.5 else 0 for p in probs]
    hits = sum(1 for a, b in zip(preds, labels) if a == b) / len(labels)
    return {
        "n": len(probs),
        "n_pairs": len(pairs),
        "brier": brier_score(probs, labels),
        "log_loss": log_loss(probs, labels),
        "hit_rate_at_50": hits,
        "reliability": calibration_bins(probs, labels),
        "message": "ok",
    }


def evaluate_fixture(fixture_id: str, *, horizon_sec: int = 300, path: Optional[str] = None) -> Dict[str, Any]:
    pairs = join_decisions_and_labels(fixture_id, horizon_sec=horizon_sec, path=path)
    metrics = evaluate_pairs(pairs)
    return {
        "fixture_id": fixture_id,
        "horizon_sec": horizon_sec,
        "metrics": metrics,
        "pairs_sample": pairs[:5],
    }


def evaluate_all_fixtures(*, horizon_sec: int = 300, path: Optional[str] = None) -> Dict[str, Any]:
    path = path or DB_PATH
    init_schema(path)
    conn = get_conn(path)
    try:
        fids = [r[0] for r in conn.execute(
            "SELECT DISTINCT fixture_id FROM labels WHERE censored=0 AND label IS NOT NULL"
        ).fetchall()]
    except Exception:
        fids = []
    conn.close()

    all_pairs: List[Dict[str, Any]] = []
    per_fx = []
    for fid in fids:
        pairs = join_decisions_and_labels(fid, horizon_sec=horizon_sec, path=path)
        all_pairs.extend(pairs)
        per_fx.append({"fixture_id": fid, "n_pairs": len(pairs)})

    overall = evaluate_pairs(all_pairs)
    return {
        "horizon_sec": horizon_sec,
        "fixtures_with_labels": len(fids),
        "per_fixture": per_fx,
        "overall": overall,
        "generated_at": time.time(),
        "note": (
            "Métricas só são significativas com volume de labels não censurados. "
            "n=0 significa falta de dados históricos limpos, não falha do motor."
        ),
    }


def synthetic_smoke_eval(n: int = 200, seed: int = 0) -> Dict[str, Any]:
    """Sanity check do pipeline de métricas com dados sintéticos (DEMO only)."""
    import random
    rng = random.Random(seed)
    pairs = []
    for i in range(n):
        p = rng.uniform(0.2, 0.8)
        y = 1 if rng.random() < p else 0
        pairs.append({"prob": p, "label": y})
    return {"demo_mode": True, "metrics": evaluate_pairs(pairs), "n": n}

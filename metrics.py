# metrics.py — Métricas honestas para avaliação de sinais
from __future__ import annotations
from typing import List, Dict, Any, Optional
import math


def brier_score(probs: List[float], outcomes: List[int]) -> float:
    """Menor é melhor. 0 = perfeito."""
    if not probs or len(probs) != len(outcomes):
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def log_loss(probs: List[float], outcomes: List[int], eps: float = 1e-7) -> float:
    if not probs or len(probs) != len(outcomes):
        return float("nan")
    s = 0.0
    for p, o in zip(probs, outcomes):
        p = min(max(p, eps), 1 - eps)
        s += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return s / len(probs)


def accuracy(preds: List[int], outcomes: List[int]) -> float:
    if not preds or len(preds) != len(outcomes):
        return float("nan")
    return sum(1 for a, b in zip(preds, outcomes) if a == b) / len(preds)


def profit_factor(pnls: List[float]) -> float:
    gains = sum(x for x in pnls if x > 0)
    losses = abs(sum(x for x in pnls if x < 0))
    if losses < 1e-12:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def max_drawdown(equity_curve: List[float]) -> float:
    """Drawdown máximo em fração (0.2 = -20%)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for x in equity_curve:
        peak = max(peak, x)
        if peak > 0:
            max_dd = max(max_dd, (peak - x) / peak)
    return max_dd


def roi(pnls: List[float], stakes: List[float]) -> float:
    total_staked = sum(stakes)
    if total_staked <= 0:
        return 0.0
    return sum(pnls) / total_staked


def calibration_bins(
    probs: List[float], outcomes: List[int], n_bins: int = 10
) -> List[Dict[str, float]]:
    """Para cada bin: prob média prevista vs taxa real de acerto."""
    bins = [[] for _ in range(n_bins)]
    for p, o in zip(probs, outcomes):
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append((p, o))
    result = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        mp = sum(p for p, _ in bucket) / len(bucket)
        mo = sum(o for _, o in bucket) / len(bucket)
        result.append({
            "bin": i,
            "n": len(bucket),
            "mean_pred": round(mp, 4),
            "mean_outcome": round(mo, 4),
            "gap": round(mp - mo, 4),
        })
    return result


def summarize_signals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    rows: cada item com
      prob, outcome (0/1), signal (BUY_*/HOLD), pnl, stake
    """
    trades = [r for r in rows if r.get("signal", "HOLD") != "HOLD"]
    if not trades:
        return {"n_trades": 0, "message": "nenhum trade"}

    probs = [float(r["prob"]) for r in trades]
    outcomes = [int(r["outcome"]) for r in trades]
    preds = [1 if r.get("signal", "").startswith("BUY") else 0 for r in trades]
    pnls = [float(r.get("pnl", 0)) for r in trades]
    stakes = [float(r.get("stake", 1)) for r in trades]

    equity = []
    cum = 0.0
    for p in pnls:
        cum += p
        equity.append(cum)

    return {
        "n_trades": len(trades),
        "n_wins": sum(outcomes),
        "hit_rate": round(sum(outcomes) / len(outcomes), 4),
        "accuracy": round(accuracy(preds, outcomes), 4),
        "brier": round(brier_score(probs, outcomes), 4),
        "log_loss": round(log_loss(probs, outcomes), 4),
        "roi": round(roi(pnls, stakes), 4),
        "profit_factor": round(profit_factor(pnls), 4),
        "max_drawdown": round(max_drawdown(equity), 4),
        "total_pnl": round(sum(pnls), 4),
        "calibration": calibration_bins(probs, outcomes),
    }

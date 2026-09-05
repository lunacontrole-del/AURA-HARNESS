"""Kelly apenas em PAPER — nunca executa stake real.

Condições obrigatórias (todas) para calcular stake de paper:
1. alvo e mercado semanticamente iguais (next_corner)
2. probabilidade presente
3. odd real timestampada e fresca
4. edge positivo
5. flags kelly_paper_enabled (não é stake real)
6. limites diários de paper

KELLY_LIVE permanece False permanentemente neste módulo.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from market_edge import compute_edge

KELLY_LIVE = False  # nunca True neste código
MAX_PAPER_STAKE_PCT = 0.02
KELLY_FRACTION = 0.25  # quarter-Kelly


def paper_kelly_stake(
    p_model: Optional[float],
    odds: Optional[float],
    *,
    market_name: Any = None,
    odds_ts: Any = None,
    bankroll_paper: float = 1000.0,
    fraction: float = KELLY_FRACTION,
    max_pct: float = MAX_PAPER_STAKE_PCT,
) -> Dict[str, Any]:
    edge_info = compute_edge(p_model, odds, market_name=market_name, odds_ts=odds_ts)
    out: Dict[str, Any] = {
        "kelly_live": KELLY_LIVE,
        "paper": True,
        "stake_pct": 0.0,
        "stake_amount": 0.0,
        "edge_info": edge_info,
        "code": "INIT",
        "message": "",
    }
    if KELLY_LIVE:
        # belt and suspenders
        out["code"] = "LIVE_FORBIDDEN"
        out["message"] = "Kelly live proibido"
        return out

    if not edge_info.get("compatible") or edge_info.get("edge") is None:
        out["code"] = edge_info.get("code") or "NO_EDGE"
        out["message"] = edge_info.get("message") or "Sem edge compatível"
        return out

    edge = float(edge_info["edge"])
    od = float(edge_info["odds"])
    p = float(edge_info["p_model"])
    if edge <= 0:
        out["code"] = "EDGE_NON_POSITIVE"
        out["message"] = "Edge ≤ 0 — paper stake 0"
        return out

    # Kelly for decimal odds: f* = (b*p - q) / b  where b=odds-1, q=1-p
    b = od - 1.0
    q = 1.0 - p
    f = (b * p - q) / b if b > 0 else 0.0
    f = max(0.0, f) * fraction
    f = min(f, max_pct)

    out["stake_pct"] = f
    out["stake_amount"] = bankroll_paper * f
    out["kelly_full"] = (b * p - q) / b if b > 0 else 0.0
    out["code"] = "PAPER_OK"
    out["message"] = "Stake de paper calculado — não executar em casa real"
    return out


class CapitalProtectionGate:
    """Freio de emergência local para paper trades; nunca libera execução real."""
    def __init__(self, *, consecutive_limit: int = 3, daily_limit: int = 5,
                 cooldown_seconds: float = 900.0, clock=None) -> None:
        import time as _time
        self.consecutive_limit = int(consecutive_limit)
        self.daily_limit = int(daily_limit)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock or _time.time
        self._date = None
        self._consecutive_losses = 0
        self._daily_losses = 0
        self._cooldown_until = 0.0

    def _reset_if_new_day(self) -> None:
        import datetime as _datetime
        today = _datetime.datetime.fromtimestamp(self._clock()).date().isoformat()
        if today != self._date:
            self._date = today
            self._consecutive_losses = 0
            self._daily_losses = 0
            self._cooldown_until = 0.0

    def can_trade(self) -> bool:
        self._reset_if_new_day()
        return (self._daily_losses < self.daily_limit and
                self._clock() >= self._cooldown_until)

    def record_loss(self) -> None:
        self._reset_if_new_day()
        self._daily_losses += 1
        self._consecutive_losses += 1
        if self._consecutive_losses >= self.consecutive_limit:
            self._cooldown_until = self._clock() + self.cooldown_seconds
            self._consecutive_losses = 0

    def record_win(self) -> None:
        self._reset_if_new_day()
        self._consecutive_losses = 0

    def status(self) -> Dict[str, Any]:
        self._reset_if_new_day()
        return {
            "paper_only": True,
            "execution_allowed": False,
            "daily_losses": self._daily_losses,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_until": self._cooldown_until,
            "blocked": not self.can_trade(),
        }


class ExecutionSlippageSimulator:
    """Aplica slippage em paper trading, com RNG injetável para reprodutibilidade."""
    def __init__(self, *, base_slippage: float = 0.02,
                 random_range=(0.01, 0.04), rng=None) -> None:
        import random as _random
        self.base_slippage = float(base_slippage)
        self.random_range = tuple(float(x) for x in random_range)
        self._rng = rng or _random.Random()
        if self.base_slippage < 0 or len(self.random_range) != 2:
            raise ValueError("parametros de slippage invalidos")

    def apply_slippage(self, target_odd: float, confidence: float) -> Dict[str, float]:
        odd = float(target_odd)
        conf = float(confidence)
        if odd <= 1.0 or not 0.0 <= conf <= 1.0:
            raise ValueError("odd deve ser > 1 e confidence entre 0 e 1")
        confidence_penalty = max(0.0, conf - 0.70) * 0.10
        random_penalty = self._rng.uniform(*self.random_range)
        total_loss = self.base_slippage + confidence_penalty + random_penalty
        executed_odd = round(max(1.01, odd * (1.0 - total_loss)), 2)
        return {
            "target_odd": odd,
            "executed_odd": executed_odd,
            "slippage_fraction": round(total_loss, 6),
            "confidence_penalty": round(confidence_penalty, 6),
            "random_penalty": round(random_penalty, 6),
            "paper_only": True,
        }


CAPITAL_GATE = CapitalProtectionGate()
SLIPPAGE_SIMULATOR = ExecutionSlippageSimulator()

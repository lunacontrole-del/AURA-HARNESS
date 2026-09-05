# -*- coding: utf-8 -*-
"""
PILAR 5 - Motor Poisson e Risco Quantitativo
AURA QUANT-X v12.7.0-RECONSOLIDADO

Implementação canônica usada pelo adaptador `pillar_runtime`. O módulo mantém
a API moderna `evaluate()` e a API de compatibilidade `approve()` descrita nos
anexos, sempre em paper trade e fail-closed.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import numpy as np

import logging
logger = logging.getLogger("aura.pilar5.poisson_risk")

MIN_PROB_THRESHOLD = 0.55
ODDS_VELOCITY_BLOCK_THRESHOLD = 1.5
ODDS_VELOCITY_CONFLUENCE = -5.0
KELLY_FRACTION = 0.25
DAILY_STOP_LOSS = -0.08
COOLDOWN_SECONDS = 120
MAX_DAILY_TRADES = 12
MAX_TRADES_PER_MATCH = 3
DISTRIBUTION_SIZE = 40


class RiskCode(Enum):
    APPROVED = "APPROVED"
    BLOCKED_NO_EDGE = "BLOCKED_NO_EDGE"
    BLOCKED_PROB_LOW = "BLOCKED_PROB_LOW"
    BLOCKED_COOLDOWN = "BLOCKED_COOLDOWN"
    BLOCKED_DAILY_LIMIT = "BLOCKED_DAILY_LIMIT"
    BLOCKED_STOP_LOSS = "BLOCKED_STOP_LOSS"
    BLOCKED_SMART_MONEY = "BLOCKED_SMART_MONEY"
    BLOCKED_NO_ODDS = "BLOCKED_NO_ODDS"
    BLOCKED_BY_MARKET = "BLOCKED_BY_MARKET"


@dataclass
class PoissonResult:
    probabilities: np.ndarray
    expected_total: float
    lambda_total: float
    over_probs: Dict[float, float]
    lambda_home: float = 0.0
    lambda_away: float = 0.0


@dataclass
class RiskDecision:
    approved: bool
    risk_reason_code: str
    edge: float
    kelly_fraction: float
    recommended_stake: float
    poisson_prob: float
    gates: Dict[str, bool] = field(default_factory=dict)
    decision: str = "BLOCKED_NO_EDGE"
    reason_code: str = ""
    reason_text: str = ""
    suggested_stake: float = 0.0

    # Enum-like compatibility constants used by the attached test contract.
    APPROVED = RiskCode.APPROVED.value
    BLOCKED_NO_EDGE = RiskCode.BLOCKED_NO_EDGE.value
    BLOCKED_PROB_LOW = RiskCode.BLOCKED_PROB_LOW.value
    BLOCKED_COOLDOWN = RiskCode.BLOCKED_COOLDOWN.value
    BLOCKED_DAILY_LIMIT = RiskCode.BLOCKED_DAILY_LIMIT.value
    BLOCKED_STOP_LOSS = RiskCode.BLOCKED_STOP_LOSS.value
    BLOCKED_SMART_MONEY = RiskCode.BLOCKED_SMART_MONEY.value
    BLOCKED_NO_ODDS = RiskCode.BLOCKED_NO_ODDS.value
    BLOCKED_BY_MARKET = RiskCode.BLOCKED_BY_MARKET.value


class PoissonEngine:
    """Motor Poisson escalar com fatoriais pré-computados."""

    def __init__(self) -> None:
        self._factorials = np.array([math.factorial(k) for k in range(DISTRIBUTION_SIZE)], dtype=np.float64)
        self._lock = threading.Lock()

    def poisson_pmf(self, k: int, lam: float) -> float:
        if k < 0 or k >= len(self._factorials) or lam < 0:
            return 0.0
        return math.exp(-lam) * (lam ** k) / float(self._factorials[k])

    def corner_over_prob(self, expected_corners: float, line: float) -> float:
        floor_line = int(math.floor(float(line)))
        prob = sum(self.poisson_pmf(k, max(0.0, float(expected_corners))) for k in range(floor_line + 1, DISTRIBUTION_SIZE))
        return min(max(prob, 0.0), 1.0)

    def implied_prob(self, odds: float):
        try:
            odds = float(odds)
        except (TypeError, ValueError):
            return None
        if odds <= 1.0:
            return None
        return 1.0 / odds


class PoissonCalculator:
    """API completa do Pilar 5 para distribuição, cache e linhas de corners."""

    def __init__(self, cache_capacity: int = 256) -> None:
        self.engine = PoissonEngine()
        self.cache_capacity = max(8, int(cache_capacity))
        self._cache: OrderedDict[float, np.ndarray] = OrderedDict()
        self._cache_hits = 0
        self._cache_misses = 0
        self._lock = threading.Lock()

    def get_distribution(self, lam: float) -> np.ndarray:
        key = round(max(0.0, float(lam)), 6)
        with self._lock:
            if key in self._cache:
                self._cache_hits += 1
                self._cache.move_to_end(key)
                return self._cache[key].copy()
            self._cache_misses += 1
        values = np.arange(DISTRIBUTION_SIZE, dtype=np.float64)
        dist = np.exp(-key) * np.power(key, values) / self.engine._factorials
        total = float(dist.sum())
        if total > 0:
            dist /= total
        with self._lock:
            self._cache[key] = dist
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_capacity:
                self._cache.popitem(last=False)
        return dist.copy()

    def calculate_corner_distribution(
        self,
        lambda_home: float,
        lambda_away: float,
        minute: int = 0,
        current_corners_home: int = 0,
        current_corners_away: int = 0,
    ) -> PoissonResult:
        minute = max(0, min(90, int(minute or 0)))
        remaining_fraction = max(0.0, (90.0 - minute) / 90.0)
        projected_home = max(0.0, float(lambda_home) * remaining_fraction)
        projected_away = max(0.0, float(lambda_away) * remaining_fraction)
        rem_home = max(0.0, projected_home - max(0, int(current_corners_home or 0))) if minute > 0 else projected_home
        rem_away = max(0.0, projected_away - max(0, int(current_corners_away or 0))) if minute > 0 else projected_away
        lambda_total = rem_home + rem_away
        expected_total = float(max(0, int(current_corners_home or 0)) + max(0, int(current_corners_away or 0)) + lambda_total)
        dist = self.get_distribution(lambda_total)
        over_probs = {line: float(dist[int(math.floor(line)) + 1 :].sum()) for line in np.arange(0.5, 20.5, 0.5)}
        return PoissonResult(
            probabilities=dist,
            expected_total=expected_total,
            lambda_total=lambda_total,
            over_probs=over_probs,
            lambda_home=rem_home,
            lambda_away=rem_away,
        )

    def get_over_probability(self, result: PoissonResult, line: float) -> float:
        if float(line) in result.over_probs:
            return result.over_probs[float(line)]
        index = int(math.floor(float(line))) + 1
        return float(result.probabilities[index:].sum()) if index < len(result.probabilities) else 0.0

    def get_stats(self) -> Dict[str, int]:
        with self._lock:
            return {"cache_hits": self._cache_hits, "cache_misses": self._cache_misses, "cache_size": len(self._cache)}


class RiskManager:
    """Gates de risco thread-safe, com API `evaluate` e `approve`."""

    def __init__(
        self,
        kelly_fraction: float = KELLY_FRACTION,
        min_prob: float = MIN_PROB_THRESHOLD,
        max_daily_loss: float = abs(DAILY_STOP_LOSS * 1000.0),
        max_trades_per_day: int = MAX_DAILY_TRADES,
        max_trades_per_match: int = MAX_TRADES_PER_MATCH,
        cooldown_seconds: int = COOLDOWN_SECONDS,
        odds_velocity_block: float = ODDS_VELOCITY_BLOCK_THRESHOLD,
    ) -> None:
        self.poisson = PoissonEngine()
        self.kelly_fraction = max(0.0, min(1.0, float(kelly_fraction)))
        self.min_prob = float(min_prob)
        self.max_daily_loss = abs(float(max_daily_loss))
        self.max_trades_per_day = max(1, int(max_trades_per_day))
        self.max_trades_per_match = max(1, int(max_trades_per_match))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.odds_velocity_block = float(odds_velocity_block)
        self._lock = threading.Lock()
        self._last_trade_ts = 0.0
        self._fixture_last_trade: Dict[str, float] = {}
        self._fixture_trades: Dict[str, int] = {}
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._day_marker = time.strftime("%Y-%m-%d")
        self._approvals = 0
        self._blocks = 0
        self._block_reasons: Dict[str, int] = {}

    def _reset_daily_if_needed(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day_marker:
            self._day_marker = today
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._fixture_trades.clear()

    def _result(
        self,
        approved: bool,
        code: str,
        reason: str,
        *,
        edge: float = 0.0,
        kelly: float = 0.0,
        stake: float = 0.0,
        poisson_prob: float = 0.0,
        gates: Optional[Dict[str, bool]] = None,
    ) -> RiskDecision:
        if approved:
            self._approvals += 1
        else:
            self._blocks += 1
            self._block_reasons[reason] = self._block_reasons.get(reason, 0) + 1
        return RiskDecision(
            approved=approved,
            risk_reason_code=code,
            edge=float(edge),
            kelly_fraction=float(kelly),
            recommended_stake=float(stake),
            poisson_prob=float(poisson_prob),
            gates=dict(gates or {}),
            decision=code,
            reason_code=reason,
            reason_text=reason,
            suggested_stake=float(stake),
        )

    def validate_trade_with_market(self, odds_velocity: Optional[float], signal: str) -> Tuple[bool, Optional[str]]:
        if str(signal or "").upper() != "BUY_CORNER":
            return False, "not_buy_signal"
        if odds_velocity is not None and float(odds_velocity) > self.odds_velocity_block:
            return False, "smart_money_divergence"
        return True, None

    def approve(
        self,
        signal_decision: str,
        corner_prob: float,
        odds: float,
        edge: float,
        fixture_id: str,
        odds_velocity: Optional[float] = None,
        current_stake: float = 1000.0,
        daily_loss: Optional[float] = None,
        **_: Any,
    ) -> RiskDecision:
        with self._lock:
            self._reset_daily_if_needed()
            signal = str(signal_decision or "").upper()
            fid = str(fixture_id or "unknown")
            if signal != "BUY_CORNER":
                return self._result(False, RiskCode.BLOCKED_NO_EDGE.value, "not_buy_signal")
            try:
                odds_f = float(odds)
                prob_f = float(corner_prob)
                edge_f = float(edge)
            except (TypeError, ValueError):
                return self._result(False, RiskCode.BLOCKED_NO_ODDS.value, "invalid_numeric_input")
            if odds_f <= 1.0:
                return self._result(False, RiskCode.BLOCKED_NO_ODDS.value, "invalid_odds")
            if odds_velocity is not None and float(odds_velocity) > self.odds_velocity_block:
                return self._result(False, RiskCode.BLOCKED_SMART_MONEY.value, "smart_money_divergence")
            if prob_f < self.min_prob:
                return self._result(False, RiskCode.BLOCKED_PROB_LOW.value, "prob_below_threshold", edge=edge_f, poisson_prob=prob_f)
            if edge_f <= 0.0:
                return self._result(False, RiskCode.BLOCKED_NO_EDGE.value, "no_edge_or_stake", edge=edge_f, poisson_prob=prob_f)
            now = time.time()
            last = self._fixture_last_trade.get(fid, 0.0)
            if now - last < self.cooldown_seconds:
                return self._result(False, RiskCode.BLOCKED_COOLDOWN.value, "cooldown", edge=edge_f, poisson_prob=prob_f)
            if self._daily_trades >= self.max_trades_per_day:
                return self._result(False, RiskCode.BLOCKED_DAILY_LIMIT.value, "daily_trade_limit", edge=edge_f, poisson_prob=prob_f)
            if self._fixture_trades.get(fid, 0) >= self.max_trades_per_match:
                return self._result(False, RiskCode.BLOCKED_DAILY_LIMIT.value, "match_trade_limit", edge=edge_f, poisson_prob=prob_f)
            loss = float(daily_loss) if daily_loss is not None else self._daily_pnl
            if loss >= self.max_daily_loss or loss <= -self.max_daily_loss:
                return self._result(False, RiskCode.BLOCKED_STOP_LOSS.value, "daily_stop_loss", edge=edge_f, poisson_prob=prob_f)
            bankroll = max(1.0, float(current_stake or 1000.0))
            b = odds_f - 1.0
            kelly_full = max(0.0, (b * prob_f - (1.0 - prob_f)) / b)
            # Margem estrita abaixo do teto de 5% para evitar arredondamento no limite.
            kelly = min(kelly_full * self.kelly_fraction, 0.0499)
            stake = bankroll * kelly
            self._last_trade_ts = now
            self._fixture_last_trade[fid] = now
            self._fixture_trades[fid] = self._fixture_trades.get(fid, 0) + 1
            self._daily_trades += 1
            return self._result(True, RiskCode.APPROVED.value, "approved", edge=edge_f, kelly=kelly, stake=stake, poisson_prob=prob_f)

    def evaluate(
        self,
        odds: float,
        expected_corners: float,
        line: float = 9.5,
        odds_velocity: Optional[float] = None,
        bankroll: float = 1000.0,
        wom_red_flag: bool = False,
    ) -> RiskDecision:
        p_model = self.poisson.corner_over_prob(float(expected_corners), float(line))
        p_implied = self.poisson.implied_prob(odds)
        if p_implied is None:
            return self._result(False, RiskCode.BLOCKED_BY_MARKET.value, "odds_unobserved", poisson_prob=p_model)
        edge = p_model - p_implied
        if wom_red_flag:
            return self._result(False, RiskCode.BLOCKED_BY_MARKET.value, "smart_money_divergence", edge=edge, poisson_prob=p_model)
        return self.approve(
            signal_decision="BUY_CORNER",
            corner_prob=p_model,
            odds=odds,
            edge=edge,
            fixture_id="evaluate",
            odds_velocity=odds_velocity,
            current_stake=bankroll,
        )

    def register_trade(self, pnl: float) -> None:
        with self._lock:
            self._last_trade_ts = time.time()
            self._daily_pnl += float(pnl)
            self._daily_trades += 1

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "approvals": self._approvals,
                "blocks": self._blocks,
                "block_reasons": dict(self._block_reasons),
                "daily_trades": self._daily_trades,
                "daily_pnl": self._daily_pnl,
            }


_rm: Optional[RiskManager] = None
_rm_lock = threading.Lock()


def get_risk_manager() -> RiskManager:
    global _rm
    with _rm_lock:
        if _rm is None:
            _rm = RiskManager()
        return _rm


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(get_risk_manager().evaluate(odds=1.90, expected_corners=10.2, line=9.5, odds_velocity=0.3))

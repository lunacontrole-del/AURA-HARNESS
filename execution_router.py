# engine/execution_router.py
from __future__ import annotations

from functools import wraps
from typing import Any, Dict
import logging

from engine.core.policy_runtime import get_system_policy

logger = logging.getLogger("aura.execution")

try:
    import ccxt  # noqa: F401
    CCXT_OK = True
except ImportError:
    CCXT_OK = False


def _hard_block(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        policy = get_system_policy()
        if policy["paper_trade"] or not policy["execution_allowed"]:
            return {
                "status": "PAPER_BLOCKED",
                "reason": "HARD_LOCK_ACTIVE",
                "simulated": True,
                "execution_allowed": False,
                "paper_trade": True,
                "mode": policy.get("mode", "paper"),
            }
        return fn(*args, **kwargs)
    return wrapper


class ExecutionRouter:
    """Roteador de execução. LIVE só com unlock completo via policy_runtime."""

    def __init__(self, exchange_id: str = "binance", paper: bool = True, api_key: str = "", secret: str = ""):
        policy = get_system_policy()
        self.paper = bool(policy["paper_trade"]) or not bool(policy["execution_allowed"])
        self.exchange = None
        self.requested_real = bool(not paper or api_key or secret)
        self.exchange_id = exchange_id
        self._api_key = api_key
        self._secret = secret

    @_hard_block
    def execute(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        policy = get_system_policy()
        safe_signal = dict(signal) if isinstance(signal, dict) else {"value": signal}

        logger.info("PAPER EXEC %s", safe_signal.get("match_id") or safe_signal.get("fixture_id"))
        return {
            "status": "PAPER_FILLED",
            "paper_trade": True,
            "real_execution_allowed": False,
            "requested_real": self.requested_real,
            "signal": safe_signal,
            "mode": "paper",
        }

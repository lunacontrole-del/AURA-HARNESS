# engine/agents/bet365_asian_stabilizer.py
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

try:
    import zmq
    ZMQ_OK = True
except ImportError:
    ZMQ_OK = False

ANSI_RED = "\033[91m"
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"
ZMQ_PORT = 5558


@dataclass
class MatchState:
    match_id: str = "SIM-BET365"
    initial_line: float = 6.5
    current_line: float = 6.5
    real_corners: int = 0
    match_minute: float = 0.0
    current_odds: float = 1.90
    opportunity: bool = False
    stabilized: bool = False
    last_alert_ts: float = 0.0
    odds_history: Deque = field(default_factory=lambda: deque(maxlen=64))

    def delta(self) -> float:
        return float(self.initial_line) - float(self.current_line)

    def opportunity_condition(self) -> bool:
        # Delta == (Linha Inicial - Linha Atual) is tautological; business rule:
        # Oportunidade when current_line == real_corners + 1
        # and delta reflects the house adjustment from initial line
        target_line = float(self.real_corners) + 1.0
        line_match = abs(float(self.current_line) - target_line) < 1e-9
        delta_ok = abs(self.delta() - (float(self.initial_line) - float(self.current_line))) < 1e-9
        return line_match and delta_ok and self.real_corners > 0

    def odds_stable(self, window_sec: float = 15.0, max_variation_pct: float = 2.0) -> bool:
        now = time.time()
        samples = [s for s in self.odds_history if now - s[0] <= window_sec]
        if len(samples) < 2:
            return False
        odds_vals = [s[1] for s in samples]
        base = odds_vals[0]
        if base <= 0:
            return False
        max_o = max(odds_vals)
        min_o = min(odds_vals)
        variation_pct = ((max_o - min_o) / base) * 100.0
        return variation_pct <= max_variation_pct

    def stabilization_trigger(self) -> bool:
        in_window = 30.0 <= float(self.match_minute) <= 40.0
        return self.opportunity_condition() and in_window and self.odds_stable(15.0, 2.0)


class MarketDataInterceptor:
    """Mock of Playwright/Mitmproxy network interception publishing ticks."""

    def __init__(self) -> None:
        self._ticks: List[Dict[str, Any]] = [
            {"match_id": "SIM-BET365", "minute": 10.0, "asian_line": 6.5, "corners": 1, "odds": 1.90},
            {"match_id": "SIM-BET365", "minute": 18.0, "asian_line": 6.5, "corners": 2, "odds": 1.88},
            {"match_id": "SIM-BET365", "minute": 25.0, "asian_line": 6.5, "corners": 3, "odds": 1.91},
            {"match_id": "SIM-BET365", "minute": 28.0, "asian_line": 6.5, "corners": 4, "odds": 1.89},
            {"match_id": "SIM-BET365", "minute": 32.0, "asian_line": 6.0, "corners": 5, "odds": 1.90},
            {"match_id": "SIM-BET365", "minute": 33.0, "asian_line": 6.0, "corners": 5, "odds": 1.91},
            {"match_id": "SIM-BET365", "minute": 34.0, "asian_line": 6.0, "corners": 5, "odds": 1.90},
            {"match_id": "SIM-BET365", "minute": 35.0, "asian_line": 6.0, "corners": 5, "odds": 1.905},
            {"match_id": "SIM-BET365", "minute": 35.5, "asian_line": 6.0, "corners": 5, "odds": 1.90},
            {"match_id": "SIM-BET365", "minute": 36.0, "asian_line": 6.0, "corners": 5, "odds": 1.90},
        ]
        self._i = 0

    async def next_tick(self) -> Optional[Dict[str, Any]]:
        if self._i >= len(self._ticks):
            return None
        tick = dict(self._ticks[self._i])
        self._i += 1
        await asyncio.sleep(0.35)
        return tick


class Bet365AsianStabilizer:
    def __init__(self, zmq_port: int = ZMQ_PORT) -> None:
        self.state = MatchState()
        self.interceptor = MarketDataInterceptor()
        self.zmq_port = zmq_port
        self._pub = None
        self._init_zmq()

    def _init_zmq(self) -> None:
        if not ZMQ_OK:
            return
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.PUB)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.bind(f"tcp://127.0.0.1:{self.zmq_port}")
        except zmq.ZMQError:
            sock.connect(f"tcp://127.0.0.1:{self.zmq_port}")
        self._pub = sock
        time.sleep(0.1)

    def update_from_tick(self, tick: Dict[str, Any]) -> None:
        st = self.state
        if st.initial_line == 6.5 and tick.get("asian_line") is not None and st.real_corners == 0:
            # lock initial line on first meaningful tick if still default and early
            pass
        if st.real_corners == 0 and st.match_minute == 0.0:
            st.initial_line = float(tick.get("asian_line", st.initial_line))
        st.match_id = str(tick.get("match_id") or st.match_id)
        st.match_minute = float(tick.get("minute") or st.match_minute)
        st.current_line = float(tick.get("asian_line") or st.current_line)
        st.real_corners = int(tick.get("corners") or st.real_corners)
        st.current_odds = float(tick.get("odds") or st.current_odds)
        st.odds_history.append((time.time(), st.current_odds))
        st.opportunity = st.opportunity_condition()
        st.stabilized = st.stabilization_trigger()

    def build_alert_payload(self) -> Dict[str, Any]:
        st = self.state
        return {
            "alert_type": "ASIAN_STABLE_ENTRY",
            "initial_line": st.initial_line,
            "current_line": st.current_line,
            "real_corners": st.real_corners,
            "match_minute": st.match_minute,
            "voice_instruction": (
                f"Atenção. Linha asiática de {int(st.current_line) if st.current_line == int(st.current_line) else st.current_line} "
                f"escanteios estabilizada aos {int(st.match_minute)} minutos. "
                "Mercado ajustado para devolução imediata. Janela de entrada aberta."
            ),
        }

    def publish_alert(self, payload: Dict[str, Any]) -> bool:
        msg = "asian_stable::" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        print(
            f"{ANSI_RED}{ANSI_BOLD}[ALERTA ESTABILIZAÇÃO BET365]{ANSI_RESET} "
            f"{ANSI_RED}{payload.get('voice_instruction')}{ANSI_RESET}"
        )
        if self._pub is None:
            print("[ZMQ] PUB indisponivel — alerta local apenas")
            return False
        try:
            self._pub.send_string(msg)
            return True
        except Exception as e:
            print(f"[ZMQ] erro: {e}")
            return False

    async def run(self) -> None:
        print("[Bet365AsianStabilizer] monitorando linha asiatica (interceptor mock)...")
        while True:
            tick = await self.interceptor.next_tick()
            if tick is None:
                # idle poll — keep consuming if external interceptor wired later
                await asyncio.sleep(1.0)
                # regenerate demo cycle optional
                self.interceptor._i = 0
                continue
            self.update_from_tick(tick)
            st = self.state
            print(
                f"[tick] min={st.match_minute:.1f} line={st.current_line} "
                f"corners={st.real_corners} odds={st.current_odds:.3f} "
                f"delta={st.delta():.2f} opp={st.opportunity} stable={st.stabilized}"
            )
            if st.stabilized and (time.time() - st.last_alert_ts) > 30.0:
                payload = self.build_alert_payload()
                # enforce exact example shape fields
                payload["alert_type"] = "ASIAN_STABLE_ENTRY"
                self.publish_alert(payload)
                st.last_alert_ts = time.time()


if __name__ == "__main__":
    asyncio.run(Bet365AsianStabilizer().run())

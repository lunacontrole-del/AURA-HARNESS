"""Hawkes process for corners — for / against (conceded) por lado.

Atualização recursiva:
  λ ← μ + (λ - μ) * exp(-β * Δt) + φ_type

Não autoriza stake. Só intensidade e P(≥1) em horizonte H.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- Hiperparâmetros default (calibráveis; conservadores) ---
MU_BASE = 0.10  # cantos/min basal por lado (~9/jogo/lado se 90min — ordem de grandeza)
HORIZON_PRIMARY_MIN = 5.0

# φ (salto) e β (1/min) por tipo — meia-vida ≈ ln2/β
TYPE_PARAMS: Dict[str, Tuple[float, float]] = {
    # (phi, beta)
    "corner_for": (0.55, 0.35),       # ~2 min half-life
    "corner_against": (0.60, 0.32),
    "dangerous": (0.22, 0.55),
    "shot": (0.18, 0.70),
    "xg": (0.15, 0.65),
    "pressure": (0.20, 0.50),
    "goal": (0.05, 1.20),             # pico curto / quase reset prático via beta alto
    "card": (0.08, 0.40),
}

GOAL_MU_MULTIPLIER = 0.55  # pós-gol: reduz fundo por um tempo (aplicado via soft factor)
GOAL_COOLDOWN_MIN = 3.0


def _f(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def p_at_least_one(lam_per_min: float, horizon_min: float) -> float:
    """P(N>=1) com taxa constante λ no horizonte (minutos)."""
    lam = max(0.0, float(lam_per_min)) * max(0.0, float(horizon_min))
    return float(1.0 - math.exp(-lam))


@dataclass
class HawkesState:
    side: str  # home | away
    lam_for: float = MU_BASE
    lam_against: float = MU_BASE  # corners conceded intensity
    t_last: float = 0.0  # match minute of last update
    mu_for: float = MU_BASE
    mu_against: float = MU_BASE
    goal_cool_until: float = -1.0  # minute until mu dampening

    def decay_to(self, minute: float) -> None:
        if self.t_last <= 0:
            self.t_last = minute
            return
        dt = max(0.0, minute - self.t_last)
        if dt <= 0:
            return
        # generic decay with mean beta ~0.4 for residual above mu
        beta = 0.40
        factor = math.exp(-beta * dt)
        self.lam_for = self.mu_for + (self.lam_for - self.mu_for) * factor
        self.lam_against = self.mu_against + (self.lam_against - self.mu_against) * factor
        self.t_last = minute

    def apply_event(self, minute: float, event_type: str, *, direction: str) -> None:
        """direction: 'for' | 'against' relative to this side."""
        self.decay_to(minute)
        phi, beta = TYPE_PARAMS.get(event_type, (0.12, 0.50))
        # instantaneous add after decay already applied with generic beta;
        # refine: apply type-specific residual decay from last type is heavy;
        # simple additive jump is enough for live.
        if direction == "for":
            self.lam_for = max(self.mu_for, self.lam_for) + phi
        else:
            self.lam_against = max(self.mu_against, self.lam_against) + phi
        if event_type == "goal":
            self.goal_cool_until = minute + GOAL_COOLDOWN_MIN
        self.t_last = minute

    def effective_mu_against(self, minute: float) -> float:
        mu = self.mu_against
        if minute < self.goal_cool_until:
            mu *= GOAL_MU_MULTIPLIER
        return mu

    def intensity_against(self, minute: float) -> float:
        self.decay_to(minute)
        # floor at effective mu
        return max(self.effective_mu_against(minute), self.lam_against)

    def intensity_for(self, minute: float) -> float:
        self.decay_to(minute)
        return max(self.mu_for, self.lam_for)


def classify_event(ev: Any) -> Tuple[Optional[str], Optional[str]]:
    """Returns (event_type, side) where side is home|away|None."""
    if isinstance(ev, dict):
        blob = " ".join(str(ev.get(k) or "") for k in ("type", "event", "name", "kind", "label")).lower()
        side_raw = str(ev.get("side") or ev.get("team") or ev.get("teamSide") or "").lower()
    else:
        blob = str(ev).lower()
        side_raw = ""

    side = None
    if side_raw in ("home", "h", "mandante", "1"):
        side = "home"
    elif side_raw in ("away", "a", "visitante", "2"):
        side = "away"
    elif "home" in side_raw:
        side = "home"
    elif "away" in side_raw:
        side = "away"

    etype = None
    if "corner" in blob or "escante" in blob or "canto" in blob:
        etype = "corner"
    elif "goal" in blob or "gol" in blob:
        etype = "goal"
    elif "shot" in blob or "chute" in blob or "remate" in blob:
        etype = "shot"
    elif "xg" in blob:
        etype = "xg"
    elif "dangerous" in blob or "press" in blob or "ataque" in blob:
        etype = "dangerous"
    elif "card" in blob or "cart" in blob or "red" in blob or "yellow" in blob:
        etype = "card"
    return etype, side


def _pair_stats(stats: Dict[str, Any], *keys: str) -> Tuple[Optional[float], Optional[float]]:
    for k in keys:
        b = stats.get(k)
        if isinstance(b, dict):
            return _f(b.get("home", b.get("h"))), _f(b.get("away", b.get("a")))
    return None, None


def build_hawkes_from_payload(
    payload: Dict[str, Any],
    *,
    analysis: Optional[Dict[str, Any]] = None,
    base_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Constrói estado Hawkes home/away for+against a partir de eventos + stats.
    """
    analysis = analysis or {}
    minute = _f(payload.get("minute", analysis.get("minute"))) or 0.0
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    if not stats and isinstance(analysis.get("stats"), dict):
        stats = analysis["stats"]

    mu = base_rate if base_rate and base_rate > 0 else MU_BASE
    # optional: pace from totals
    c_h, c_a = _pair_stats(stats, "corners", "escanteios")
    if minute >= 10 and c_h is not None and c_a is not None:
        # rate per side from match so far (mild prior blend)
        mu_h = 0.6 * mu + 0.4 * (c_h / max(1.0, minute))
        mu_a = 0.6 * mu + 0.4 * (c_a / max(1.0, minute))
    else:
        mu_h = mu_a = mu

    home = HawkesState(side="home", lam_for=mu_h, lam_against=mu_a, mu_for=mu_h, mu_against=mu_a, t_last=0.0)
    away = HawkesState(side="away", lam_for=mu_a, lam_against=mu_h, mu_for=mu_a, mu_against=mu_h, t_last=0.0)

    events = payload.get("events") or payload.get("eventos") or analysis.get("matchEvents") or []
    if not isinstance(events, list):
        events = []

    # sort by minute when possible
    def ev_min(ev: Any) -> float:
        if isinstance(ev, dict):
            m = _f(ev.get("minute", ev.get("min", ev.get("clock"))))
            return m if m is not None else 0.0
        return 0.0

    sorted_events = sorted(events, key=ev_min)

    for ev in sorted_events:
        etype, side = classify_event(ev)
        if not etype:
            continue
        m = ev_min(ev)
        if etype == "corner" and side:
            # corner for `side` = against the other
            if side == "home":
                home.apply_event(m, "corner_for", direction="for")
                away.apply_event(m, "corner_against", direction="against")
            else:
                away.apply_event(m, "corner_for", direction="for")
                home.apply_event(m, "corner_against", direction="against")
        elif etype in ("dangerous", "shot", "xg", "pressure") and side:
            # pressure by side = against the other
            if side == "home":
                home.apply_event(m, etype if etype != "pressure" else "dangerous", direction="for")
                away.apply_event(m, etype if etype != "pressure" else "dangerous", direction="against")
            else:
                away.apply_event(m, etype if etype != "pressure" else "dangerous", direction="for")
                home.apply_event(m, etype if etype != "pressure" else "dangerous", direction="against")
        elif etype == "goal" and side:
            if side == "home":
                home.apply_event(m, "goal", direction="for")
                away.apply_event(m, "goal", direction="against")
            else:
                away.apply_event(m, "goal", direction="for")
                home.apply_event(m, "goal", direction="against")
        elif etype == "card":
            # weak signal both if side unknown
            if side == "home":
                home.apply_event(m, "card", direction="for")
            elif side == "away":
                away.apply_event(m, "card", direction="for")

    # inject current pressure levels as soft jumps if no recent events but DA high
    da_h, da_a = _pair_stats(stats, "dangerous", "dangerousAttacks", "ataques_perigosos")
    if da_h is not None and da_a is not None and minute > 0:
        # relative pressure share as mild continuous driver
        tot = da_h + da_a + 1e-6
        # boost against intensity proportional to opponent DA rate
        home.decay_to(minute)
        away.decay_to(minute)
        home.lam_against = max(home.lam_against, home.mu_against + 0.15 * (da_a / max(1.0, minute)))
        away.lam_against = max(away.lam_against, away.mu_against + 0.15 * (da_h / max(1.0, minute)))
        home.lam_for = max(home.lam_for, home.mu_for + 0.15 * (da_h / max(1.0, minute)))
        away.lam_for = max(away.lam_for, away.mu_for + 0.15 * (da_a / max(1.0, minute)))

    lam_h_for = home.intensity_for(minute)
    lam_a_for = away.intensity_for(minute)
    lam_h_conc = home.intensity_against(minute)  # corners conceded by home
    lam_a_conc = away.intensity_against(minute)

    # match intensity = sum of for (equivalent to sum of against)
    lam_match = lam_h_for + lam_a_for

    H = HORIZON_PRIMARY_MIN
    windows = {}
    for name, h in (("1m", 1.0), ("3m", 3.0), ("5m", 5.0), ("10m", 10.0)):
        windows[name] = {
            "match": p_at_least_one(lam_match, h),
            "home_for": p_at_least_one(lam_h_for, h),
            "away_for": p_at_least_one(lam_a_for, h),
            "home_conceded": p_at_least_one(lam_h_conc, h),
            "away_conceded": p_at_least_one(lam_a_conc, h),
        }

    # defensive stress: high λ_conceded + long gap since last conceded corner
    def stress(lam_c: float, side_label: str) -> Dict[str, Any]:
        level = "low"
        if lam_c >= 0.35:
            level = "high"
        elif lam_c >= 0.20:
            level = "medium"
        return {
            "side": side_label,
            "lambda_conceded": round(lam_c, 5),
            "level": level,
            "p_conceded_5m": p_at_least_one(lam_c, 5.0),
        }

    return {
        "product": "hawkes_corners_v1",
        "minute": minute,
        "mu": {"home_for": mu_h, "away_for": mu_a},
        "lambda": {
            "home_for": round(lam_h_for, 5),
            "away_for": round(lam_a_for, 5),
            "home_conceded": round(lam_h_conc, 5),
            "away_conceded": round(lam_a_conc, 5),
            "match": round(lam_match, 5),
        },
        "windows": windows,
        "defensive_stress": {
            "home": stress(lam_h_conc, "home"),
            "away": stress(lam_a_conc, "away"),
        },
        "primary_horizon_min": H,
        "p_match_5m": windows["5m"]["match"],
        "events_used": len(sorted_events),
        "params": {"type_params": TYPE_PARAMS, "mu_base": MU_BASE},
        "generated_at": time.time(),
        "disclaimer": "Hawkes for/against — análise; não é ordem de aposta.",
    }

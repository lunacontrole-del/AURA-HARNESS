# engine/point_in_time.py — V23 strict PIT anti data-leakage
from __future__ import annotations
from typing import Any, Callable, Dict, Optional

try:
    import pandas as pd
except Exception:
    pd = None  # type: ignore


def build_strict_point_in_time_dataset(
    raw_events_df: Any,
    window_start_ts: float,
    feature_fn: Optional[Callable[[Any], Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Features so podem usar eventos com received_ts < window_start_ts.
    Label (futuro) NUNCA entra nas features.
    """
    if pd is None or raw_events_df is None:
        return None
    try:
        df = raw_events_df
        if not hasattr(df, "columns"):
            return None
        if "received_ts" not in df.columns:
            return None
        past = df[df["received_ts"] < float(window_start_ts)]
        if past is None or len(past) == 0:
            return None
        if feature_fn is not None:
            features = feature_fn(past)
        else:
            features = {
                "n_events": int(len(past)),
                "last_ts": float(past["received_ts"].max()) if len(past) else None,
            }
        return {
            "features": features,
            "window_start_ts": float(window_start_ts),
            "n_past_events": int(len(past)),
            "pit_ok": True,
            "paper_trade": True,
            "execution_allowed": False,
        }
    except Exception:
        return None


def assert_no_future_leak(feature_ts: float, window_start_ts: float) -> bool:
    """Retorna True se feature_ts e estritamente anterior ao inicio da janela."""
    try:
        return float(feature_ts) < float(window_start_ts)
    except Exception:
        return False

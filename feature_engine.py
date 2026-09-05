# engine/agents/feature_engine.py
"""FeatureEngine V2 — contexto temporal (deltas) para o GLM. Stdlib only."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class MatchTimeline:
    fixture_id: str
    snapshots: List[Tuple[float, Dict]] = field(default_factory=list)
    last_analysis: Optional[Dict] = None
    last_conclusion: Optional[str] = None

    def push(self, view: Dict) -> None:
        self.snapshots.append((time.time(), dict(view)))
        if len(self.snapshots) > 30:
            self.snapshots = self.snapshots[-30:]

    def delta(self, metric: str, window_min: int = 10) -> Optional[Tuple[int, int]]:
        if len(self.snapshots) < 2:
            return None
        now_ts, now_view = self.snapshots[-1]
        cutoff = now_ts - (window_min * 60)
        ref_view = None
        for ts, v in self.snapshots:
            if ts >= cutoff:
                ref_view = v
                break
        if ref_view is None:
            ref_view = self.snapshots[0][1]

        def get_pair(view: Dict, key: str):
            return (
                int(view.get(f"{key}_home", 0) or 0),
                int(view.get(f"{key}_away", 0) or 0),
            )

        now_pair = get_pair(now_view, metric)
        ref_pair = get_pair(ref_view, metric)
        return (now_pair[0] - ref_pair[0], now_pair[1] - ref_pair[1])


class FeatureEngine:
    def __init__(self, timelines: Dict[str, MatchTimeline]):
        self.timelines = timelines

    def build_features(self, view: Dict) -> Dict:
        fid = str(view.get("fixture_id") or view.get("match_id") or "unknown")
        tl = self.timelines.get(fid)
        if tl is None:
            tl = MatchTimeline(fixture_id=fid)
            self.timelines[fid] = tl
        tl.push(view)

        minute = int(view.get("minute") or 0)
        feats: Dict = {"minute": minute, "fixture_id": fid}

        for metric in ("dangerous", "attacks", "corners"):
            d = tl.delta(metric, window_min=10)
            if d:
                feats[f"delta_{metric}_10min"] = {"home": d[0], "away": d[1]}
                total = d[0] + d[1]
                feats[f"{metric}_trend"] = (
                    "rising" if total >= 4 else "stable" if total >= 1 else "cooling"
                )

        events = view.get("corner_events") or []
        if events and minute:
            last = events[-1]
            if isinstance(last, dict):
                last_min = last.get("minute", last.get("m", 0))
            elif isinstance(last, (list, tuple)) and last:
                last_min = last[0]
            else:
                last_min = 0
            try:
                gap = max(0, int(minute) - int(last_min or 0))
            except (TypeError, ValueError):
                gap = 0
            feats["corner_gap_min"] = gap
            feats["corner_excitation"] = round(2.718 ** (-gap / 8.0), 3)

        if events:
            recent = []
            for e in events:
                try:
                    em = e.get("minute", 0) if isinstance(e, dict) else (e[0] if e else 0)
                    if minute - int(em or 0) <= 15:
                        recent.append(e)
                except Exception:
                    pass
            feats["corner_rate_15min"] = len(recent)

        dh = int(view.get("dangerous_home") or view.get("dangerous_attacks") or 0)
        da = int(view.get("dangerous_away") or 0)
        total_d = dh + da
        if total_d > 0:
            feats["pressure_asymmetry"] = round((dh - da) / total_d, 3)

        # pressure features from tracker if present
        pf = view.get("pressure_features") or {}
        for k in ("pressure_ma", "pressure_delta", "dang_rate_10m", "is_noise"):
            if k in pf:
                feats[k] = pf[k]
            elif k in view:
                feats[k] = view[k]

        if tl.last_conclusion:
            feats["previous_conclusion"] = tl.last_conclusion
        return feats

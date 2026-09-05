# engine/agents/memory_store.py
"""MemoryStore V2 — memoria persistente + calibracao. Stdlib only."""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


class MemoryStore:
    def __init__(self, path: Path = Path("engine/data/glm_memory.json")):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not dict")
                # ensure keys
                raw.setdefault("trigger_stats", {})
                raw.setdefault("league_profiles", {})
                raw.setdefault("validated_patterns", [])
                raw.setdefault(
                    "calibration",
                    {"predictions": 0, "correct": 0, "by_confidence_band": {}},
                )
                return raw
            except Exception:
                pass
        return {
            "trigger_stats": {},
            "league_profiles": {},
            "validated_patterns": [],
            "calibration": {
                "predictions": 0,
                "correct": 0,
                "by_confidence_band": {},
            },
        }

    def record_decision(
        self,
        fixture_id: str,
        triggers: List[str],
        confidence: float,
        decision: str,
    ) -> None:
        with self._lock:
            key = "+".join(sorted(triggers)) if triggers else "none"
            ts = self._data["trigger_stats"].setdefault(key, {"hits": 0, "total": 0})
            ts["total"] = int(ts.get("total", 0)) + 1
            band = f"{int(float(confidence) * 10) / 10:.1f}"
            bands = self._data["calibration"].setdefault("by_confidence_band", {})
            b = bands.setdefault(band, {"n": 0, "hits": 0})
            b["n"] = int(b.get("n", 0)) + 1

    def record_outcome(
        self,
        fixture_id: str,
        triggers: List[str],
        confidence: float,
        was_correct: bool,
    ) -> None:
        with self._lock:
            key = "+".join(sorted(triggers)) if triggers else "none"
            ts = self._data["trigger_stats"].setdefault(key, {"hits": 0, "total": 0})
            if was_correct:
                ts["hits"] = int(ts.get("hits", 0)) + 1
                self._data["calibration"]["correct"] = (
                    int(self._data["calibration"].get("correct", 0)) + 1
                )
            self._data["calibration"]["predictions"] = (
                int(self._data["calibration"].get("predictions", 0)) + 1
            )
            band = f"{int(float(confidence) * 10) / 10:.1f}"
            bands = self._data["calibration"].setdefault("by_confidence_band", {})
            b = bands.setdefault(band, {"n": 0, "hits": 0})
            if was_correct:
                b["hits"] = int(b.get("hits", 0)) + 1
            self._flush()

    def trigger_reliability(self, triggers: List[str]) -> Optional[float]:
        key = "+".join(sorted(triggers)) if triggers else None
        if not key:
            return None
        ts = self._data["trigger_stats"].get(key)
        if not ts or int(ts.get("total", 0)) < 5:
            return None
        return float(ts["hits"]) / float(ts["total"])

    def calibration_report(self) -> Dict:
        out: Dict[str, Any] = {}
        bands = self._data.get("calibration", {}).get("by_confidence_band", {})
        for band, v in bands.items():
            n = int(v.get("n", 0))
            if n >= 10:
                out[band] = {
                    "declared": float(band),
                    "actual": round(int(v.get("hits", 0)) / n, 3),
                    "n": n,
                }
        return out

    def best_patterns(self, min_n: int = 10, min_rate: float = 0.6) -> List[Dict]:
        results = []
        for key, ts in self._data.get("trigger_stats", {}).items():
            total = int(ts.get("total", 0))
            hits = int(ts.get("hits", 0))
            if total >= min_n and hits / total >= min_rate:
                results.append(
                    {"triggers": key, "rate": round(hits / total, 3), "n": total}
                )
        return sorted(results, key=lambda x: -x["rate"])[:10]

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # serialize trigger_stats as plain dict
        payload = {
            "trigger_stats": dict(self._data.get("trigger_stats") or {}),
            "league_profiles": dict(self._data.get("league_profiles") or {}),
            "validated_patterns": list(self._data.get("validated_patterns") or []),
            "calibration": {
                "predictions": self._data.get("calibration", {}).get("predictions", 0),
                "correct": self._data.get("calibration", {}).get("correct", 0),
                "by_confidence_band": dict(
                    self._data.get("calibration", {}).get("by_confidence_band") or {}
                ),
            },
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

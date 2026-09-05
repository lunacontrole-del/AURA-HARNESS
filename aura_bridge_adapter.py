"""Adapter AURA V23 <-> CornerWindowSpecialist (advisory-only, paper trade).

Nao executa ordens. Fonte de verdade continua sendo /api/ui/state + DOM.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, Optional

from corner_window_specialist import CornerWindowSpecialist

_MEM = Path(__file__).resolve().parent / "corner_pattern_memory.json"
_specialist: Optional[CornerWindowSpecialist] = None


def get_specialist() -> CornerWindowSpecialist:
    global _specialist
    if _specialist is None:
        _specialist = CornerWindowSpecialist(memory_path=str(_MEM) if _MEM.exists() else None)
    return _specialist


def analyze_from_ui_state(ui_state: Dict[str, Any]) -> Dict[str, Any]:
    """Converte payload /api/ui/state em decisao OBSERVE|PREPARE|NO_BET."""
    snap = ui_state.get("snapshot") if isinstance(ui_state.get("snapshot"), dict) else {}
    view = snap.get("view") if isinstance(snap.get("view"), dict) else snap
    row = {
        "minute": view.get("minute") if isinstance(view, dict) else None,
        "period": view.get("period") or view.get("status") if isinstance(view, dict) else None,
        "data_quality": snap.get("data_quality") or ui_state.get("data_quality") or None,
        "corners_home": (view.get("corners") or [None, None])[0] if isinstance(view, dict) else None,
        "corners_away": (view.get("corners") or [None, None])[1] if isinstance(view, dict) else None,
        "score_home": (view.get("score") or [None, None])[0] if isinstance(view, dict) else None,
        "score_away": (view.get("score") or [None, None])[1] if isinstance(view, dict) else None,
        "dangerous": view.get("dangerous") if isinstance(view, dict) else None,
        "attacks": view.get("attacks") if isinstance(view, dict) else None,
        "fixture_id": ui_state.get("fixtureId"),
    }
    # flatten nested lists if needed
    if isinstance(row.get("dangerous"), list) and len(row["dangerous"]) >= 2:
        row["dangerous_home"] = row["dangerous"][0]
        row["dangerous_away"] = row["dangerous"][1]
    try:
        result = get_specialist().evaluate(row)
    except Exception as e:
        result = {
            "decision": "NO_BET",
            "reason": f"adapter_error:{type(e).__name__}",
            "error": str(e),
        }
    if not isinstance(result, dict):
        result = {"decision": "NO_BET", "raw": result}
    result["paper_trade"] = True
    result["execution_allowed"] = False
    result["advisory_only"] = True
    result["source"] = "corner_independent_v7"
    return result

# engine/dom_canary.py — V23 self-healing selector advisory
from __future__ import annotations
import logging
from typing import Any, Dict, List

logger = logging.getLogger("aura.dom_canary")


def check_canary(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    advisories: List[Dict[str, Any]] = []
    if not isinstance(snapshot, dict):
        return advisories
    view = snapshot.get("view") if isinstance(snapshot.get("view"), dict) else snapshot
    checks = {
        "score": view.get("score") if isinstance(view, dict) else None,
        "corners": view.get("corners") if isinstance(view, dict) else None,
        "home": view.get("home") if isinstance(view, dict) else None,
        "away": view.get("away") if isinstance(view, dict) else None,
    }
    for target, value in checks.items():
        if value is None or value == "" or value == [None, None]:
            adv = {
                "agent": "self_healing_scraper",
                "msg": f"Seletor de {target.upper()} retornou nulo. SokkerPro pode ter mudado o layout.",
                "action": "SUGGEST_NEW_SELECTOR",
                "target": f"{target}_element",
                "paper_trade": True,
                "execution_allowed": False,
            }
            advisories.append(adv)
            logger.warning("DOM_CANARY_DEAD target=%s", target)
    return advisories

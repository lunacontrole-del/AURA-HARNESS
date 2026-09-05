# engine/permission_gate.py — P1 Ultra: assert de politica imutavel
from __future__ import annotations
import os

def assert_paper_only(gate: str = "default") -> None:
    """Fail-closed: exige paper_trade e bloqueia execução real."""
    if os.getenv("AURA_PAPER_ONLY", "1") != "1":
        raise RuntimeError(f"[AURA:{gate}] paper_trade!=true — abortando (fail-closed)")
    if os.getenv("AURA_EXECUTION_ALLOWED", "0") != "0":
        raise RuntimeError(f"[AURA:{gate}] execution_allowed!=false — abortando (fail-closed)")

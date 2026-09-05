# Corner Window Specialist V7 — AURA Quant-X
# Advisory-only. SHADOW_PAPER_TRADE. execution_allowed always False.
from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "corner_window_specialist",
    Path(__file__).resolve().parent / "corner_window_specialist.py",
)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
CornerWindowSpecialist = _mod.CornerWindowSpecialist

__all__ = ["CornerWindowSpecialist"]

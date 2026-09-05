"""GLM inference gate DISABLED — stub symbols for imports."""
from __future__ import annotations

from typing import Any


class _DummyLock:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


GLM_INFERENCE_LOCK = _DummyLock()
GLM_RESOURCE_GOVERNOR = None


def wait_for_glm_resources(*a, **k) -> bool:
    return False


class GlmInferenceGate:
    enabled = False

    def __init__(self, *a, **k):
        self.enabled = False

    def health(self) -> dict[str, Any]:
        return {"ok": False, "glm_enabled": False, "reason": "disabled_v26_full"}

    def infer(self, *a, **k) -> dict[str, Any]:
        return {"status": "GLM_DISABLED", "use": "hermes"}


__all__ = [
    "GLM_INFERENCE_LOCK",
    "GLM_RESOURCE_GOVERNOR",
    "wait_for_glm_resources",
    "GlmInferenceGate",
]

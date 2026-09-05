"""GLM runtime DISABLED in V26-FULL — stub API-compatible."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class AgentEvent:
    def __init__(self, *a, **k):
        pass


class Advisory:
    def __init__(self, *a, **k):
        pass


class AgentGLMRuntime:
    """Stub: GLM desativado. API compativel com engine/server.py."""

    enabled = False

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else Path(".")
        self.enabled = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def enqueue(
        self,
        source_agent: str,
        event_type: str,
        summary: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "GLM_DISABLED",
            "execution_allowed": False,
            "use": "hermes",
        }

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "glm_enabled": False,
            "status": "DISABLED",
            "queue_depth": 0,
            "busy": False,
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return []


# aliases legados
AgentGlmRuntime = AgentGLMRuntime
GLM_INFERENCE_LOCK = None

__all__ = ["AgentGLMRuntime", "AgentGlmRuntime", "AgentEvent", "Advisory", "GLM_INFERENCE_LOCK"]

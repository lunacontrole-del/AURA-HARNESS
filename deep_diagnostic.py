"""Diagnóstico profundo, read-only e sanitizado do AURA Quant-X.

Este módulo não executa comandos recebidos do GLM, não grava no banco e não
retorna caminhos locais, tokens, cookies ou credenciais. O uso de nvidia-smi é
fixo e somente para leitura; ausência do binário é uma degradação normal.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "engine" / "aura_quant_x.db"

SERVICES = {
    "bridge": "http://127.0.0.1:8080/health",
    "engine": "http://127.0.0.1:8765/health",
    "voice": "http://127.0.0.1:8099/api/voice/health",
    "ollama": "http://127.0.0.1:11434/api/tags",
}


def _safe_error(exc: BaseException) -> str:
    return type(exc).__name__


def _service_snapshot(*, self_liveness: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, url in SERVICES.items():
        if name == "engine" and self_liveness:
            # A rota /api/diagnostics/deep só pode executar se o próprio
            # processo do Engine já estiver vivo e aceitando requisições.
            # Evita uma chamada HTTP recursiva ao mesmo servidor, que pode
            # consumir o worker disponível e produzir ReadTimeout falso.
            result[name] = {
                "state": "ONLINE",
                "http_status": 200,
                "latency_ms": 0,
                "source": "engine_request_liveness",
            }
            continue
        started = time.monotonic()
        try:
            response = requests.get(url, timeout=2.0)
            result[name] = {
                "state": "ONLINE" if response.status_code == 200 else "ERROR",
                "http_status": int(response.status_code),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except requests.RequestException as exc:
            result[name] = {
                "state": "OFFLINE",
                "error": _safe_error(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            result[name] = {"state": "ERROR", "error": _safe_error(exc)}
    return result


def _gpu_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
        if completed.returncode != 0:
            return {"available": False, "state": "UNAVAILABLE", "reason": "nvidia_smi_failed"}
        first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
        values = [item.strip() for item in first_line.split(",")]
        if len(values) != 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", item) for item in values):
            return {"available": False, "state": "UNAVAILABLE", "reason": "nvidia_smi_unparsed"}
        used, total, utilization = (float(item) for item in values)
        return {
            "available": True,
            "state": "ONLINE",
            "memory_used_mb": used,
            "memory_total_mb": total,
            "utilization_percent": utilization,
        }
    except FileNotFoundError:
        return {"available": False, "state": "UNAVAILABLE", "reason": "nvidia_smi_missing"}
    except Exception as exc:
        return {"available": False, "state": "ERROR", "reason": _safe_error(exc)}


def _database_snapshot() -> dict[str, Any]:
    if not DB_PATH.is_file():
        return {"available": False, "state": "ABSENT"}
    try:
        size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 3)
        counts: dict[str, int | None] = {}
        with sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=2) as connection:
            for table in ("paper_trades", "logs_telemetria", "kb_team_alphas", "user_feedback"):
                try:
                    counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[table] = None
        return {"available": True, "state": "ONLINE", "size_mb": size_mb, "row_counts": counts}
    except (OSError, sqlite3.Error) as exc:
        return {"available": False, "state": "ERROR", "reason": _safe_error(exc)}


def collect_diagnostic(*, self_liveness: bool = False) -> dict[str, Any]:
    """Retorna um snapshot limitado e somente leitura para revisão humana/GLM."""
    services = _service_snapshot(self_liveness=self_liveness)
    online = [name for name, item in services.items() if item.get("state") == "ONLINE"]
    return {
        "ok": True,
        "diagnostic": "AURA_DEEP_READ_ONLY_V1",
        "timestamp_unix": int(time.time()),
        "policy": "READ_ONLY_NO_COMMAND_EXECUTION",
        "paper_trade_only": True,
        "platform": {
            "system": platform.system(),
            "release": platform.release()[:80],
            "architecture": platform.machine()[:40],
            "python": platform.python_version(),
        },
        "services": services,
        "services_online": online,
        "gpu": _gpu_snapshot(),
        "database": _database_snapshot(),
        "execution_allowed": False,
        "approval_required_for_mutation": True,
    }


def collect_diagnostic_json() -> str:
    return json.dumps(collect_diagnostic(), ensure_ascii=False, separators=(",", ":"), default=str)


__all__ = ["collect_diagnostic", "collect_diagnostic_json"]


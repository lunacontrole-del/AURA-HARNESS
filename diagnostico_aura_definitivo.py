"""Diagnóstico definitivo do AURA Quant-X, somente leitura.

Este utilitário consolida Deep Diagnostic, benchmark, serviços locais,
Ollama, estado do Engine, governor e memória bounded. Não executa comandos
recebidos, não altera configuração e não grava no banco. A opção --output
apenas salva explicitamente o relatório gerado pelo usuário.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
import sys
ENGINE_DIR = Path(__file__).resolve().parent
for _path in (ROOT, ENGINE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from deep_diagnostic import collect_diagnostic  # noqa: E402
from scripts.aura_performance_benchmark import collect_snapshot  # noqa: E402


_READ_ONLY = "READ_ONLY_NO_COMMAND_EXECUTION"
_ENDPOINTS = {
    "engine_status": "http://127.0.0.1:8765/api/status",
    "agent_glm": "http://127.0.0.1:8765/api/agents/glm/status",
    "performance": "http://127.0.0.1:8765/api/diagnostics/performance",
}
_SENSITIVE_WORDS = ("token", "secret", "password", "cookie", "credential", "authorization", "api_key", "private_key")


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(word in key_text.lower() for word in _SENSITIVE_WORDS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _redact(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value[:32]]
    if isinstance(value, str):
        return value[:4_000]
    return value


def _engine_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, url in _ENDPOINTS.items():
        started = time.monotonic()
        try:
            response = requests.get(url, timeout=2.0)
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text[:1_000]}
            result[name] = {
                "state": "ONLINE" if response.status_code == 200 else "ERROR",
                "http_status": int(response.status_code),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "body": _redact(body),
            }
        except requests.RequestException as exc:
            result[name] = {"state": "OFFLINE", "error": type(exc).__name__, "latency_ms": int((time.monotonic() - started) * 1000)}
        except Exception as exc:
            result[name] = {"state": "ERROR", "error": type(exc).__name__}
    return result


def collect_definitive() -> dict[str, Any]:
    deep = collect_diagnostic()
    benchmark = collect_snapshot()
    engine = _engine_snapshot()
    online = [name for name, item in deep.get("services", {}).items() if item.get("state") == "ONLINE"]
    warnings: list[str] = []
    if not deep.get("gpu", {}).get("available"):
        warnings.append("GPU_TELEMETRY_UNAVAILABLE")
    if engine.get("engine_status", {}).get("state") != "ONLINE":
        warnings.append("ENGINE_STATUS_UNAVAILABLE")
    if engine.get("agent_glm", {}).get("state") != "ONLINE":
        warnings.append("AGENT_GLM_STATUS_UNAVAILABLE")
    return {
        "ok": True,
        "diagnostic": "AURA_DEFINITIVE_READ_ONLY_V1",
        "timestamp_unix": int(time.time()),
        "policy": _READ_ONLY,
        "paper_trade_only": True,
        "execution_allowed": False,
        "services_online": online,
        "services": deep.get("services", {}),
        "hardware": {
            "platform": deep.get("platform", {}),
            "host": benchmark.get("host", {}),
            "gpu": benchmark.get("gpu", {}),
        },
        "ollama": deep.get("services", {}).get("ollama", {}),
        "database": deep.get("database", {}),
        "engine": engine,
        "comparison": {
            "benchmark": benchmark,
            "note": "Amostra pontual; não prova causalidade, estabilidade ou ausência de BSOD.",
        },
        "warnings": warnings,
        "no_mutation": True,
        "approval_required_for_mutation": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emitir JSON compacto")
    parser.add_argument("--output", help="Salvar explicitamente o relatório neste caminho")
    args = parser.parse_args()
    payload = collect_definitive()
    text = json.dumps(payload, ensure_ascii=False, indent=0 if args.json else 2, default=str)
    print(text)
    if args.output:
        output = Path(args.output).expanduser()
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"RELATORIO_SALVO={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

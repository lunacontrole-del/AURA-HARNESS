#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes Swarm V32 — multi-agent diagnostic layer for AURA QUANT-X
Paper-only. Never sets execution_allowed=true.
Extends Hermes Supervisor with specialized probes:
  Capture · Grounding · GPU · Agents · Policy · Feed quality · Self-heal hints
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "Hermes-Swarm-V32.1.0"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _port(port: int, host: str = "127.0.0.1", t: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=t):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return None


@dataclass
class SwarmFinding:
    agent: str
    severity: str  # OK | LOW | MEDIUM | HIGH | CRITICAL
    code: str
    message: str
    hint: str = ""


@dataclass
class SwarmReport:
    version: str = VERSION
    timestamp: str = field(default_factory=_now)
    score: int = 100
    status: str = "OK"
    findings: List[SwarmFinding] = field(default_factory=list)
    agents_run: List[str] = field(default_factory=list)
    invariants: Dict[str, str] = field(default_factory=dict)

    def add(self, agent: str, severity: str, code: str, message: str, hint: str = "") -> None:
        self.findings.append(SwarmFinding(agent, severity, code, message, hint))

    def finalize(self) -> None:
        score = 100
        for f in self.findings:
            if f.severity == "CRITICAL":
                score -= 15
            elif f.severity == "HIGH":
                score -= 8
            elif f.severity == "MEDIUM":
                score -= 4
            elif f.severity == "LOW":
                score -= 1
        self.score = max(0, min(100, score))
        if self.score >= 90:
            self.status = "OK"
        elif self.score >= 70:
            self.status = "DEGRADED"
        else:
            self.status = "CRITICAL"


class HermesSwarm:
    """Runs specialized agent probes and aggregates for Hermes."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or os.environ.get("AURA_ROOT") or Path.cwd()).resolve()
        os.environ.setdefault("PAPER_TRADE", "true")
        os.environ.setdefault("EXECUTION_ALLOWED", "false")
        os.environ.setdefault("GLM_ADVISORY_ONLY", "true")

    def agent_policy(self, report: SwarmReport) -> None:
        report.agents_run.append("policy")
        pt = os.environ.get("PAPER_TRADE", "").lower()
        ex = os.environ.get("EXECUTION_ALLOWED", "true").lower()
        report.invariants = {
            "paper_trade": os.environ.get("PAPER_TRADE", "N/D"),
            "execution_allowed": os.environ.get("EXECUTION_ALLOWED", "N/D"),
        }
        if pt != "true":
            report.add("policy", "CRITICAL", "PAPER_OFF", "paper_trade != true", "Restaurar PAPER_TRADE=true")
        else:
            report.add("policy", "OK", "PAPER_OK", "paper_trade=true")
        if ex not in ("false", "0"):
            report.add("policy", "CRITICAL", "EXEC_ON", "execution_allowed ativo", "Forçar EXECUTION_ALLOWED=false")
        else:
            report.add("policy", "OK", "EXEC_OFF", "execution_allowed=false")

    def agent_ports(self, report: SwarmReport) -> None:
        report.agents_run.append("ports")
        for port, name, crit in (
            (8080, "Bridge", True),
            (8765, "Engine", True),
            (8099, "Voice", False),
            (11434, "Ollama", False),
            (8766, "MatrizHTTP", False),
        ):
            up = _port(port)
            sev = "OK" if up else ("CRITICAL" if crit else "LOW")
            report.add("ports", sev, f"PORT_{port}", f"{name}:{port} {'UP' if up else 'DOWN'}",
                       "" if up else f"Subir {name}")

    def agent_capture(self, report: SwarmReport) -> None:
        report.agents_run.append("capture")
        live = self.root / "bridge" / "live_latest.json"
        if not live.exists():
            report.add("capture", "HIGH", "NO_LIVE", "live_latest.json ausente",
                       "SokkerPRO AO VIVO na Matriz/Desktop")
            return
        age = time.time() - live.stat().st_mtime
        try:
            data = json.loads(live.read_text(encoding="utf-8", errors="replace") or "{}")
        except Exception:
            data = {}
        has_teams = bool(data.get("home") or data.get("away") or data.get("teams"))
        events = data.get("corner_events") or data.get("events") or []
        if age > 120:
            report.add("capture", "HIGH", "STALE", f"live_latest age={int(age)}s",
                       "F5 no SokkerPRO AO VIVO")
        elif age > 45:
            report.add("capture", "MEDIUM", "WARM", f"live_latest age={int(age)}s",
                       "Manter jogo ao vivo visivel")
        else:
            report.add("capture", "OK", "FRESH", f"live_latest age={int(age)}s")
        if not has_teams:
            report.add("capture", "MEDIUM", "NO_TEAMS", "sem times no live_latest",
                       "Abrir fixture completa no SokkerPRO")
        n = len(events) if isinstance(events, list) else 0
        if n == 0:
            report.add("capture", "LOW", "NO_CORNERS", "corner_events vazio (inicio de jogo OK)",
                       "Aguardar corners ou outro jogo")
        else:
            report.add("capture", "OK", "CORNERS", f"{n} corner_events")

    def agent_gpu(self, report: SwarmReport) -> None:
        report.agents_run.append("gpu")
        cuda = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        num = os.environ.get("OLLAMA_NUM_GPU", "")
        if cuda == "" and num == "":
            report.add("gpu", "LOW", "GPU_ENV", "CUDA/OLLAMA_NUM_GPU nao definidos no processo",
                       "Rodar AURA_FORCE_GPU_DEDICADA.ps1 e reiniciar Ollama")
        else:
            report.add("gpu", "OK", "GPU_ENV", f"CUDA_VISIBLE_DEVICES={cuda!r} OLLAMA_NUM_GPU={num!r}")
        # optional nvidia-smi
        try:
            import subprocess
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                report.add("gpu", "OK", "NVIDIA_SMI", r.stdout.strip().split("\n")[0][:80])
            else:
                report.add("gpu", "LOW", "NO_SMI", "nvidia-smi indisponivel ou sem GPU NVIDIA")
        except Exception:
            report.add("gpu", "LOW", "NO_SMI", "nvidia-smi nao executavel")

    def agent_specialists(self, report: SwarmReport) -> None:
        report.agents_run.append("specialists")
        enabled = self.root / "agents" / "ENABLED"
        if not enabled.exists():
            report.add("specialists", "MEDIUM", "NO_ENABLED", "pasta agents/ENABLED ausente")
            return
        flags = list(enabled.glob("*.enabled"))
        report.add("specialists", "OK", "ENABLED_COUNT", f"{len(flags)} flags .enabled")
        priority = [
            "corner", "hawkes", "monte", "veracity", "drift", "market_edge",
            "gpu_resource", "browser", "health_score",
        ]
        names = " ".join(p.name.lower() for p in flags)
        for key in priority:
            if key in names:
                report.add("specialists", "OK", f"HAS_{key.upper()}", f"flag relacionada a {key}")
            else:
                report.add("specialists", "LOW", f"MISS_{key.upper()}", f"sem flag explicita {key}",
                           "Ver agents/ENABLED")

    def agent_ui_health(self, report: SwarmReport) -> None:
        report.agents_run.append("ui")
        if _port(8766):
            report.add("ui", "OK", "MATRIZ_HTTP", "Matriz HTTP :8766 UP")
        else:
            report.add("ui", "MEDIUM", "MATRIZ_DOWN", "Matriz HTTP :8766 DOWN",
                       "AURA_TUDO_AUTOMATICO.bat ou orchestrator")
        h = _http_json("http://127.0.0.1:8765/api/health") or _http_json("http://127.0.0.1:8765/health")
        if h is not None:
            report.add("ui", "OK", "ENGINE_HEALTH", "Engine health respondeu")
        elif _port(8765):
            report.add("ui", "LOW", "ENGINE_NO_JSON", "Engine UP mas health JSON falhou")
        b = _http_json("http://127.0.0.1:8080/health")
        if b is not None:
            report.add("ui", "OK", "BRIDGE_HEALTH", "Bridge health OK")

    def agent_domain_lock(self, report: SwarmReport) -> None:
        report.agents_run.append("domain")
        p = self.root / "engine" / "prompts" / "system_hermes_football_only.txt"
        if p.exists():
            report.add("domain", "OK", "FOOTBALL_PROMPT", "system_hermes_football_only.txt presente")
        else:
            report.add("domain", "HIGH", "NO_PROMPT", "prompt futebol ausente",
                       "scripts/hermes_domain_lock.py --apply")

    def run(self) -> SwarmReport:
        report = SwarmReport()
        self.agent_policy(report)
        self.agent_ports(report)
        self.agent_capture(report)
        self.agent_gpu(report)
        self.agent_specialists(report)
        self.agent_ui_health(report)
        self.agent_domain_lock(report)
        report.finalize()
        return report

    def persist(self, report: SwarmReport) -> Path:
        logdir = self.root / "logs_supervisor"
        logdir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": report.version,
            "timestamp": report.timestamp,
            "score": report.score,
            "status": report.status,
            "invariants": report.invariants,
            "agents_run": report.agents_run,
            "findings": [asdict(f) for f in report.findings],
        }
        path = logdir / "HERMES_SWARM_LATEST.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "=" * 64,
            f"HERMES SWARM {report.version}",
            f"Status: {report.status}  score={report.score}",
            f"Time: {report.timestamp}",
            f"Invariants: {report.invariants}",
            f"Agents: {', '.join(report.agents_run)}",
            "-" * 64,
        ]
        for f in report.findings:
            lines.append(f"  [{f.severity:8}] [{f.agent}] {f.code}: {f.message}")
            if f.hint:
                lines.append(f"             -> {f.hint}")
        lines.append("=" * 64)
        txt = logdir / "HERMES_SWARM_LATEST.txt"
        txt.write_text("\n".join(lines), encoding="utf-8")
        return txt

    def format_print(self, report: SwarmReport) -> str:
        lines = [
            f"HERMES SWARM {report.version} | {report.status} score={report.score}",
            f"Agents: {', '.join(report.agents_run)}",
        ]
        for f in report.findings:
            if f.severity != "OK":
                lines.append(f"  [{f.severity}] {f.agent}/{f.code}: {f.message}")
        return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    root = Path.cwd()
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            root = Path(argv[i + 1])
    swarm = HermesSwarm(root=root)
    report = swarm.run()
    path = swarm.persist(report)
    print(swarm.format_print(report))
    print(f"Report: {path}")
    return 0 if report.status != "CRITICAL" else 1


if __name__ == "__main__":
    sys.exit(main())

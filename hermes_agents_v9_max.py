#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes V9/V10 MAX Agents — inspired by top GitHub patterns (2026):
  - CrewAI role crews (Scanner/Knowledge/Fixer/Validator/Reporter/Sentinel)
  - LangGraph: stateful cycle, verify-before-claim, fail-closed
  - OpenSRE / multi-agent AIOps: Monitor -> RCA -> SafeHeal -> Report
  - custom-agent-harness: cooldown, layered self-heal, never silent failure
  - TradingAgents pattern: multi-agent debate ADVISORY ONLY (corners paper-trade)

NEVER sets execution_allowed=true. paper_trade=true always.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "Hermes-Agents-V9-MAX-1.1.0-CLEAN-INSTALL"
COOLDOWN_SEC = 45  # fail-closed: no rapid unsafe restarts


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
class Msg:
    """Crew-style inter-agent message (TradingAgents / CrewAI pattern)."""
    frm: str
    to: str
    kind: str  # finding | advice | action | verify | report | watch
    body: str
    severity: str = "INFO"


@dataclass
class Finding:
    agent: str
    severity: str
    code: str
    message: str
    rca: str = ""
    safe_action: str = ""
    verified: bool = False


@dataclass
class MaxReport:
    version: str = VERSION
    timestamp: str = field(default_factory=_now)
    score: int = 100
    status: str = "OK"
    findings: List[Finding] = field(default_factory=list)
    messages: List[Msg] = field(default_factory=list)
    agents_run: List[str] = field(default_factory=list)
    invariants: Dict[str, str] = field(default_factory=dict)
    sources: List[str] = field(default_factory=lambda: [
        "CrewAI role crews",
        "LangGraph fail-closed + verify-before-claim",
        "OpenSRE / multi-agent AIOps RCA",
        "custom-agent-harness cooldown",
        "TradingAgents debate (advisory only)",
        "OpenSRE Tracer-Cloud patterns",
    ])

    def add_finding(self, f: Finding) -> None:
        self.findings.append(f)

    def add_msg(self, frm: str, to: str, kind: str, body: str, severity: str = "INFO") -> None:
        self.messages.append(Msg(frm, to, kind, body, severity))

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
        self.status = "OK" if self.score >= 90 else ("DEGRADED" if self.score >= 70 else "CRITICAL")


class HermesAgentsV9Max:
    """
    Role crew for Hermes:
      Scanner -> Knowledge -> Fixer -> Validator -> Reporter
      Sentinel watches continuously (watch messages)
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or os.environ.get("AURA_ROOT") or Path.cwd()).resolve()
        os.environ.setdefault("PAPER_TRADE", "true")
        os.environ.setdefault("EXECUTION_ALLOWED", "false")
        os.environ.setdefault("GLM_ADVISORY_ONLY", "true")
        self._last_heal: Dict[str, float] = {}
        self.policy = self._load_trained_policy()

    def _load_trained_policy(self) -> Dict[str, Any]:
        try:
            from engine.agents.hermes_v9_trainer import HermesV9Trainer
            pol = HermesV9Trainer.load_policy(self.root)
            return pol or {}
        except Exception:
            # fallback local file
            for rel in ("engine/data/hermes_v9_trained_policy.json", "logs_supervisor/hermes_v9_trained_policy.json"):
                fp = self.root / rel
                if fp.exists():
                    try:
                        return json.loads(fp.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            return {}

    def _preferred_action(self, code: str, default: str) -> str:
        for row in (self.policy.get("priority_codes") or []):
            if row.get("code") == code and row.get("preferred_action"):
                return str(row["preferred_action"])
        return default

    def _cooldown_ok(self, key: str) -> bool:
        last = self._last_heal.get(key, 0)
        return (time.time() - last) >= COOLDOWN_SEC

    def _mark_heal(self, key: str) -> None:
        self._last_heal[key] = time.time()

    # ----- Role: Scanner (Monitor Agent / AIOps) -----
    def role_scanner(self, report: MaxReport) -> Dict[str, Any]:
        report.agents_run.append("Scanner")
        snap: Dict[str, Any] = {"ports": {}, "health": {}, "capture": {}}
        for port, name in ((8080, "bridge"), (8765, "engine"), (8099, "voice"),
                           (11434, "ollama"), (8766, "matriz")):
            snap["ports"][name] = _port(port)
        snap["health"]["bridge"] = _http_json("http://127.0.0.1:8080/health") is not None
        snap["health"]["engine"] = (
            _http_json("http://127.0.0.1:8765/api/health") is not None
            or _http_json("http://127.0.0.1:8765/health") is not None
        )
        live = self.root / "bridge" / "live_latest.json"
        if not live.exists():
            # Auto-seed demo fixture for clean installs (paper-safe)
            try:
                live.parent.mkdir(parents=True, exist_ok=True)
                demo = {
                    "mode": "paper_demo",
                    "status": "idle",
                    "source": "auto_seed_v9",
                    "timestamp": _now(),
                    "home": "Demo Home FC",
                    "away": "Demo Away United",
                    "teams": ["Demo Home FC", "Demo Away United"],
                    "score": {"home": 0, "away": 0},
                    "minute": 0,
                    "period": "PRE_MATCH",
                    "corner_events": [],
                    "events": [],
                    "corners": {"home": 0, "away": 0, "total": 0},
                    "note": "Auto-seed para instalacao limpa. Substituido pela captura real.",
                }
                live.write_text(json.dumps(demo, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        if live.exists():
            age = time.time() - live.stat().st_mtime
            try:
                data = json.loads(live.read_text(encoding="utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            is_demo = data.get("mode") in ("paper_demo", "idle") or str(data.get("source", "")).startswith(("seed", "auto_seed"))
            snap["capture"] = {
                "age_s": int(age),
                "has_teams": bool(data.get("home") or data.get("away") or data.get("teams")),
                "corners": len(data.get("corner_events") or data.get("events") or []),
                "is_demo": is_demo,
                "status": data.get("status") or data.get("period") or "unknown",
            }
        else:
            snap["capture"] = {"age_s": None, "has_teams": False, "corners": 0, "is_demo": True}

        report.add_msg("Scanner", "Knowledge", "finding", json.dumps(snap, ensure_ascii=False)[:500])
        return snap

    # ----- Role: Knowledge (RCA / RAG-style reasoning without external net) -----
    def role_knowledge(self, report: MaxReport, snap: Dict[str, Any]) -> List[Finding]:
        report.agents_run.append("Knowledge")
        findings: List[Finding] = []

        pt = os.environ.get("PAPER_TRADE", "").lower()
        ex = os.environ.get("EXECUTION_ALLOWED", "true").lower()
        report.invariants = {
            "paper_trade": os.environ.get("PAPER_TRADE", "N/D"),
            "execution_allowed": os.environ.get("EXECUTION_ALLOWED", "N/D"),
        }
        if pt != "true":
            findings.append(Finding("Knowledge", "CRITICAL", "PAPER_OFF",
                                    "paper_trade != true", "InvariantGate fail",
                                    "Restaurar PAPER_TRADE=true", False))
        if ex not in ("false", "0"):
            findings.append(Finding("Knowledge", "CRITICAL", "EXEC_ON",
                                    "execution_allowed ativo", "InvariantGate fail",
                                    "Forçar EXECUTION_ALLOWED=false", False))

        ports = snap.get("ports", {})
        if not ports.get("bridge"):
            findings.append(Finding("Knowledge", "CRITICAL", "BRIDGE_DOWN",
                                    "Bridge :8080 DOWN", "Core path broken",
                                    "safe_start bridge (orchestrator)", False))
        if not ports.get("engine"):
            findings.append(Finding("Knowledge", "CRITICAL", "ENGINE_DOWN",
                                    "Engine :8765 DOWN", "Core path broken",
                                    "safe_start engine (orchestrator)", False))

        cap = snap.get("capture", {})
        age = cap.get("age_s")
        if age is None:
            findings.append(Finding("Knowledge", "HIGH", "NO_LIVE",
                                    "live_latest ausente", "CAPTURE_ONLY",
                                    "SokkerPRO AO VIVO na Matriz", False))
        elif age > 120:
            findings.append(Finding("Knowledge", "HIGH", "STALE",
                                    f"live_latest age={age}s", "CAPTURE_ONLY",
                                    "F5 SokkerPRO AO VIVO", False))
        elif age > 45:
            findings.append(Finding("Knowledge", "MEDIUM", "WARM",
                                    f"live_latest age={age}s", "Captura aquecendo",
                                    "Manter jogo visivel", False))

        is_demo = cap.get("is_demo", False)
        if not cap.get("has_teams"):
            sev = "INFO" if is_demo or cap.get("age_s") is None else "MEDIUM"
            findings.append(Finding("Knowledge", sev, "NO_TEAMS",
                                    "sem times no live_latest" if not is_demo else "fixture demo (instalacao limpa)",
                                    "LIVE_DATA_PARTIAL" if not is_demo else "Aguardando captura real SokkerPRO/Bridge",
                                    "Abrir fixture completa SokkerPRO" if not is_demo else "Iniciar Desktop + Bridge com jogo ao vivo", False))
        corners = cap.get("corners") or 0
        if corners == 0:
            sev = "INFO" if is_demo else "LOW"
            findings.append(Finding("Knowledge", sev, "NO_CORNERS",
                                    "corner_events vazio" if not is_demo else "demo pre-match (sem corners)",
                                    "Inicio de jogo ou feed parcial" if not is_demo else "Esperado em instalacao limpa / pre-match",
                                    "Aguardar corners" if not is_demo else "Aguardar captura real", False))

        if not ports.get("matriz"):
            findings.append(Finding("Knowledge", "MEDIUM", "MATRIZ_DOWN",
                                    "Matriz HTTP :8766 DOWN", "UI path",
                                    "orchestrator sobe http.server 8766", False))

        prompt = self.root / "engine" / "prompts" / "system_hermes_football_only.txt"
        if not prompt.exists():
            findings.append(Finding("Knowledge", "HIGH", "NO_DOMAIN_LOCK",
                                    "prompt futebol ausente", "LLM domain risk",
                                    "hermes_domain_lock.py --apply", False))


        # Apply trained policy preferred actions
        for f in findings:
            if f.safe_action:
                f.safe_action = self._preferred_action(f.code, f.safe_action)

        for f in findings:
            report.add_finding(f)
            report.add_msg("Knowledge", "Fixer", "advice", f"{f.code}: {f.rca or f.message}", f.severity)
        return findings

    # ----- Role: Fixer (SafeHeal only — fail-closed) -----
    def role_fixer(self, report: MaxReport, findings: List[Finding]) -> List[str]:
        report.agents_run.append("Fixer")
        actions: List[str] = []
        for f in findings:
            if f.severity not in ("CRITICAL", "HIGH"):
                continue
            if not f.safe_action:
                continue
            key = f.code
            if not self._cooldown_ok(key):
                actions.append(f"COOLDOWN skip {key}")
                report.add_msg("Fixer", "Validator", "action", f"cooldown {key}", "LOW")
                continue
            # Only safe local file/env fixes — never kill production randomly without orchestrator
            if f.code == "NO_DOMAIN_LOCK":
                try:
                    from pathlib import Path
                    p = self.root / "engine" / "prompts"
                    p.mkdir(parents=True, exist_ok=True)
                    (p / "system_hermes_football_only.txt").write_text(
                        "Dominio: futebol/escanteios/SokkerPRO paper-trade only.\n"
                        "PROIBIDO: bolsa, tickers, execution real.\n"
                        "paper_trade=true execution_allowed=false\n",
                        encoding="utf-8",
                    )
                    actions.append("APPLIED domain_lock prompt")
                    self._mark_heal(key)
                    f.verified = True
                except Exception as e:
                    actions.append(f"FAIL domain_lock: {e}")
            else:
                actions.append(f"HINT_ONLY {key}: {f.safe_action}")
            report.add_msg("Fixer", "Validator", "action", actions[-1], f.severity)
        return actions

    # ----- Role: Validator (verify-before-claim) -----
    def role_validator(self, report: MaxReport, findings: List[Finding]) -> None:
        report.agents_run.append("Validator")
        # Re-probe critical ports
        for f in findings:
            if f.code == "BRIDGE_DOWN":
                f.verified = _port(8080)
            elif f.code == "ENGINE_DOWN":
                f.verified = _port(8765)
            elif f.code == "NO_DOMAIN_LOCK":
                f.verified = (self.root / "engine" / "prompts" / "system_hermes_football_only.txt").exists()
            elif f.code in ("NO_LIVE", "STALE", "WARM"):
                live = self.root / "bridge" / "live_latest.json"
                f.verified = live.exists() and (time.time() - live.stat().st_mtime) < 45
            report.add_msg("Validator", "Reporter", "verify",
                           f"{f.code} verified={f.verified}", "INFO")

    # ----- Role: Reporter -----
    def role_reporter(self, report: MaxReport) -> Path:
        report.agents_run.append("Reporter")
        report.finalize()
        logdir = self.root / "logs_supervisor"
        logdir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": report.version,
            "timestamp": report.timestamp,
            "score": report.score,
            "status": report.status,
            "invariants": report.invariants,
            "agents_run": report.agents_run,
            "sources": report.sources,
            "findings": [asdict(f) for f in report.findings],
            "messages": [asdict(m) for m in report.messages],
        }
        jp = logdir / "HERMES_V9_MAX_LATEST.json"
        jp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "=" * 64,
            f"HERMES V9 MAX {report.version}",
            f"Status: {report.status}  score={report.score}",
            f"Time: {report.timestamp}",
            f"Invariants: {report.invariants}",
            f"Crew: {' -> '.join(report.agents_run)}",
            f"Sources: {', '.join(report.sources[:4])}...",
            "-" * 64,
            "--- FINDINGS ---",
        ]
        for f in report.findings:
            lines.append(f"  [{f.severity:8}] {f.code}: {f.message}")
            if f.rca:
                lines.append(f"             RCA: {f.rca}")
            if f.safe_action:
                lines.append(f"             SAFE: {f.safe_action}")
            lines.append(f"             verified={f.verified}")
        lines.append("--- SWARM MESSAGES ---")
        for m in report.messages:
            lines.append(f"  {m.frm}->{m.to} [{m.kind}] {m.body[:100]}")
        lines.append("=" * 64)
        tp = logdir / "HERMES_V9_MAX_LATEST.txt"
        tp.write_text("\n".join(lines), encoding="utf-8")
        report.add_msg("Reporter", "ALL", "report", f"score={report.score} status={report.status}")
        return tp

    # ----- Role: Sentinel -----
    def role_sentinel(self, report: MaxReport) -> None:
        report.agents_run.append("Sentinel")
        report.add_msg("Sentinel", "ALL", "watch",
                       "Monitor continuo: use --loop no supervisor; paper_trade locked")

    def run(self) -> Tuple[MaxReport, Path]:
        report = MaxReport()
        snap = self.role_scanner(report)
        findings = self.role_knowledge(report, snap)
        self.role_fixer(report, findings)
        self.role_validator(report, findings)
        path = self.role_reporter(report)
        self.role_sentinel(report)
        return report, path


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    root = Path.cwd()
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            root = Path(argv[i + 1])
    crew = HermesAgentsV9Max(root=root)
    report, path = crew.run()
    print(f"HERMES V9 MAX | {report.status} score={report.score}")
    print(f"Crew: {' -> '.join(report.agents_run)}")
    for f in report.findings:
        if f.severity != "OK":
            print(f"  [{f.severity}] {f.code}: {f.message}")
    print(f"Report: {path}")
    return 0 if report.status != "CRITICAL" else 1


if __name__ == "__main__":
    sys.exit(main())

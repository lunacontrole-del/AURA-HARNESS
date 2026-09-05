#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes V9 Trainer — treino LOCAL dos agentes de diagnostico
============================================================
NAO e fine-tune de LLM de mercado. E treino operacional:

1) Le historico de relatorios Hermes (runbooks, swarm, v9 max, deep)
2) Extrai padroes (codigo de falha -> acao que precedeu melhoria de score)
3) Atualiza policy treinada (pesos + runbook preferido + hints)
4) Agentes V9 MAX carregam a policy no proximo ciclo

Invariantes: paper_trade=true | execution_allowed=false
Nunca treina para executar ordens reais.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "Hermes-V9-Trainer-1.0.0"
POLICY_NAME = "hermes_v9_trained_policy.json"
MIN_SAMPLES = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


class HermesV9Trainer:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or os.environ.get("AURA_ROOT") or Path.cwd()).resolve()
        os.environ.setdefault("PAPER_TRADE", "true")
        os.environ.setdefault("EXECUTION_ALLOWED", "false")
        self.logdir = self.root / "logs_supervisor"
        self.datadir = self.root / "engine" / "data"
        self.datadir.mkdir(parents=True, exist_ok=True)
        self.logdir.mkdir(parents=True, exist_ok=True)

    def collect_corpus(self) -> Dict[str, Any]:
        """Recolhe todos os sinais de treino disponiveis no disco."""
        corpus = {
            "reports_json": [],
            "runbooks": [],
            "text_reports": [],
            "scores": [],
        }

        patterns_json = [
            "HERMES_V9_MAX_LATEST.json",
            "HERMES_SWARM_LATEST.json",
            "HERMES_DEEP_LATEST.json",
            "hermes_supervisor_report.json",
        ]
        for name in patterns_json:
            p = self.logdir / name
            if not p.exists():
                p2 = self.datadir / name
                p = p2 if p2.exists() else p
            data = _safe_json(p) if p.exists() else None
            if data:
                corpus["reports_json"].append({"path": str(p), "data": data})
                sc = data.get("score")
                if isinstance(sc, (int, float)):
                    corpus["scores"].append(float(sc))

        # history jsonl
        hist = self.datadir / "hermes_supervisor_history.jsonl"
        if hist.exists():
            for line in _read_text(hist).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    corpus["reports_json"].append({"path": str(hist), "data": row})
                    sc = row.get("score") or (row.get("report") or {}).get("score")
                    if isinstance(sc, (int, float)):
                        corpus["scores"].append(float(sc))
                except Exception:
                    pass

        # runbooks
        rb_dir = self.logdir / "runbooks"
        if rb_dir.is_dir():
            for p in sorted(rb_dir.glob("runbook_*.md"))[-30:]:
                corpus["runbooks"].append({"path": str(p), "text": _read_text(p)[:8000]})

        for name in ("HERMES_V9_MAX_LATEST.txt", "HERMES_SWARM_LATEST.txt",
                     "HERMES_DEEP_LATEST.txt", "HERMES_AUTONOMOUS_LATEST.txt"):
            p = self.logdir / name
            if p.exists():
                corpus["text_reports"].append({"path": str(p), "text": _read_text(p)[:8000]})

        return corpus

    def extract_failure_codes(self, corpus: Dict[str, Any]) -> Counter:
        codes: Counter = Counter()
        # From structured findings
        for item in corpus["reports_json"]:
            data = item["data"]
            findings = data.get("findings") or []
            if isinstance(findings, list):
                for f in findings:
                    if not isinstance(f, dict):
                        continue
                    code = f.get("code") or f.get("id") or ""
                    sev = str(f.get("severity", "")).upper()
                    if code and sev in ("CRITICAL", "HIGH", "MEDIUM", "DEGRADED"):
                        codes[str(code)] += 2 if sev == "CRITICAL" else 1
            # nested report
            rep = data.get("report") or {}
            for f in rep.get("findings") or []:
                if isinstance(f, dict) and f.get("code"):
                    codes[str(f["code"])] += 1

        # From text
        code_re = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
        interesting = {
            "BRIDGE_DOWN", "ENGINE_DOWN", "LIVE_STALE", "STALE", "NO_LIVE",
            "LIVE_DATA_PARTIAL", "NO_TEAMS", "NO_CORNERS", "CAPTURE_ONLY",
            "PORT_8765_OFF", "CORE_DOWN", "MATRIZ_DOWN", "NO_DOMAIN_LOCK",
            "PAPER_OFF", "EXEC_ON", "BLOCKED_BY_DATA",
        }
        for item in corpus["text_reports"] + corpus["runbooks"]:
            for m in code_re.findall(item.get("text") or ""):
                if m in interesting:
                    codes[m] += 1
        return codes

    def extract_action_hints(self, corpus: Dict[str, Any]) -> Dict[str, Counter]:
        """Mapa codigo -> hints/acoes mencionadas."""
        mapping: Dict[str, Counter] = defaultdict(Counter)
        default_hints = {
            "BRIDGE_DOWN": "safe_start_bridge via orchestrator",
            "ENGINE_DOWN": "safe_start_engine via orchestrator",
            "PORT_8765_OFF": "safe_start_engine",
            "CORE_DOWN": "AURA_TUDO_AUTOMATICO.bat",
            "STALE": "SokkerPRO AO VIVO + F5 na Matriz",
            "LIVE_STALE": "SokkerPRO AO VIVO + F5 na Matriz",
            "NO_LIVE": "Abrir SokkerPRO AO VIVO no Desktop/Matriz",
            "NO_TEAMS": "Abrir fixture completa no SokkerPRO",
            "LIVE_DATA_PARTIAL": "Captura Desktop + jogo ao vivo",
            "CAPTURE_ONLY": "Nao restart core; so captura",
            "MATRIZ_DOWN": "orchestrator HTTP :8766",
            "NO_DOMAIN_LOCK": "hermes_domain_lock.py --apply",
            "PAPER_OFF": "PAPER_TRADE=true",
            "EXEC_ON": "EXECUTION_ALLOWED=false",
            "NO_CORNERS": "Aguardar corners ou outro jogo ao vivo",
        }
        for code, hint in default_hints.items():
            mapping[code][hint] += 3  # prior forte

        hint_re = re.compile(r"(?:SAFE|HINT|->|Acao|Ação|fix|recomend)", re.I)
        for item in corpus["text_reports"] + corpus["runbooks"]:
            text = item.get("text") or ""
            for line in text.splitlines():
                if not hint_re.search(line):
                    continue
                for code in default_hints:
                    if code in line:
                        clean = line.strip()[:160]
                        mapping[code][clean] += 1
        return mapping

    def build_policy(self, corpus: Dict[str, Any]) -> Dict[str, Any]:
        codes = self.extract_failure_codes(corpus)
        hints = self.extract_action_hints(corpus)
        scores = corpus["scores"]
        avg_score = sum(scores) / len(scores) if scores else None

        # severity weights trained by frequency (more frequent = higher priority)
        total = sum(codes.values()) or 1
        priority = []
        for code, cnt in codes.most_common(40):
            priority.append({
                "code": code,
                "count": cnt,
                "weight": round(cnt / total, 4),
                "preferred_action": hints[code].most_common(1)[0][0] if hints[code] else "",
                "alt_actions": [a for a, _ in hints[code].most_common(3)],
            })

        policy = {
            "version": VERSION,
            "trained_at": _now(),
            "paper_trade": True,
            "execution_allowed": False,
            "samples": {
                "reports_json": len(corpus["reports_json"]),
                "runbooks": len(corpus["runbooks"]),
                "text_reports": len(corpus["text_reports"]),
                "score_points": len(scores),
                "avg_score": avg_score,
            },
            "priority_codes": priority,
            "agent_roles": [
                "Scanner", "Knowledge", "Fixer", "Validator", "Reporter", "Sentinel"
            ],
            "training_type": "operational_experience_replay",
            "notes": (
                "Treino local por experiencia de diagnosticos. "
                "Nao e modelo preditivo de corners. "
                "Nao habilita execucao real."
            ),
        }
        return policy

    def save_policy(self, policy: Dict[str, Any]) -> Path:
        path = self.datadir / POLICY_NAME
        path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        # also mirror in logs
        (self.logdir / POLICY_NAME).write_text(
            json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def train(self) -> Tuple[Dict[str, Any], Path]:
        corpus = self.collect_corpus()
        n = (
            len(corpus["reports_json"])
            + len(corpus["runbooks"])
            + len(corpus["text_reports"])
        )
        policy = self.build_policy(corpus)
        path = self.save_policy(policy)
        policy["_corpus_size"] = n
        return policy, path

    @staticmethod
    def load_policy(root: Path) -> Optional[Dict[str, Any]]:
        for p in (
            root / "engine" / "data" / POLICY_NAME,
            root / "logs_supervisor" / POLICY_NAME,
        ):
            if p.exists():
                data = _safe_json(p)
                if isinstance(data, dict):
                    return data
        return None


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    root = Path.cwd()
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            root = Path(argv[i + 1])

    print(f"=== {VERSION} ===")
    print(f"ROOT={root}")
    print("Invariants: paper_trade=true execution_allowed=false")
    print("Tipo: treino operacional (experiencia de diagnosticos), NAO fine-tune de odds")

    trainer = HermesV9Trainer(root=root)
    policy, path = trainer.train()
    samples = policy.get("samples", {})
    print(f"Samples: {samples}")
    print(f"Priority codes: {len(policy.get('priority_codes', []))}")
    for row in policy.get("priority_codes", [])[:8]:
        print(f"  {row['code']}: weight={row['weight']} action={row['preferred_action'][:60]}")
    print(f"Policy salva: {path}")
    print("Proximos ciclos do Hermes V9 MAX usarao esta policy automaticamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

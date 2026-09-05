#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ThresholdTuner — propõe ajustes para o dia seguinte.
NUNCA sobrescreve config ativa sem --promote (com backup).
Mudancas limitadas por max_step.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TUNER] %(levelname)s: %(message)s",
)
logger = logging.getLogger("aura.tuner")

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# path bootstrap when run as script
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from agents.feedback_connector import (
        DecisionRecord,
        OutcomeResolver,
        RegParser,
        RegSnapshot,
    )
except ImportError:
    try:
        from engine.agents.feedback_connector import (
            DecisionRecord,
            OutcomeResolver,
            RegParser,
            RegSnapshot,
        )
    except ImportError:
        # same-dir import
        from feedback_connector import (  # type: ignore
            DecisionRecord,
            OutcomeResolver,
            RegParser,
            RegSnapshot,
        )


@dataclass
class Tunable:
    name: str
    kind: str
    current: float
    default: float
    min_value: float
    max_value: float
    max_step: float
    target_precision: float = 0.60
    target_recall: float = 0.50
    min_sample: int = 10
    rationale: str = ""
    new_value: Optional[float] = None
    delta: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)


TUNABLES_DEFAULT: List[Dict] = [
    dict(
        name="trend_rising_threshold",
        kind="int",
        default=4,
        min_value=2,
        max_value=8,
        max_step=1,
        rationale="Delta AP 10min para rising.",
    ),
    dict(
        name="excitation_threshold",
        kind="float",
        default=0.30,
        min_value=0.15,
        max_value=0.55,
        max_step=0.05,
        rationale="Hawkes min para jogo quente.",
    ),
    dict(
        name="corner_rate_threshold",
        kind="int",
        default=2,
        min_value=1,
        max_value=4,
        max_step=1,
        rationale="Min cantos 15min ritmo alto.",
    ),
    dict(
        name="entra_min_triggers",
        kind="int",
        default=2,
        min_value=1,
        max_value=3,
        max_step=1,
        rationale="Min gatilhos para ENTRA.",
    ),
    dict(
        name="entra_min_confidence",
        kind="float",
        default=0.70,
        min_value=0.50,
        max_value=0.85,
        max_step=0.05,
        rationale="Confianca minima ENTRA.",
    ),
    dict(
        name="entra_min_score",
        kind="int",
        default=70,
        min_value=55,
        max_value=85,
        max_step=5,
        rationale="Score minimo ENTRA.",
    ),
]


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            return yaml.safe_load(text) or {}
        except Exception:
            pass
    # minimal key: value parser
    out: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        try:
            if "." in v:
                out[k] = float(v)
            else:
                out[k] = int(v)
        except ValueError:
            if v.lower() in ("true", "false"):
                out[k] = v.lower() == "true"
            else:
                out[k] = v
    return out


def _dump_yaml(data: Dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    lines = []
    for k, v in data.items():
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


class EvidenceCollector:
    def __init__(self, decisions_file: Path, regs_dir: Path, lookback_days: int = 7):
        self.decisions_file = decisions_file
        self.regs_dir = regs_dir
        self.lookback = lookback_days

    def collect(self) -> Tuple[List[DecisionRecord], List[RegSnapshot]]:
        cutoff = (datetime.now() - timedelta(days=self.lookback)).strftime("%Y-%m-%d")
        decisions: List[DecisionRecord] = []
        if self.decisions_file.exists():
            for line in self.decisions_file.read_text(encoding="utf-8").splitlines():
                try:
                    j = json.loads(line)
                    if j.get("ts", "") >= cutoff:
                        decisions.append(
                            DecisionRecord(
                                ts=j["ts"],
                                fixture_id=j.get("fixture_id"),
                                home=j.get("home"),
                                away=j.get("away"),
                                minute=j.get("minute"),
                                window=j.get("window", "out"),
                                decision=j.get("decision", "AGUARDA"),
                                confidence=float(j.get("confidence") or 0.0),
                                triggers=list(j.get("triggers") or []),
                                kills=list(j.get("kills") or []),
                            )
                        )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        snapshots: List[RegSnapshot] = []
        if self.regs_dir.exists():
            for p in sorted(self.regs_dir.glob("REG-*.md")):
                try:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
                except OSError:
                    continue
                if mtime >= cutoff:
                    s = RegParser.parse_file(p)
                    if s:
                        snapshots.append(s)
        OutcomeResolver(decisions, snapshots).resolve_all()
        return decisions, snapshots


class ThresholdTuner:
    def __init__(
        self,
        config_path: Path,
        decisions_file: Path,
        regs_dir: Path,
        output_dir: Path = Path("engine/data/tuning"),
    ):
        self.config_path = Path(config_path)
        self.decisions_file = Path(decisions_file)
        self.regs_dir = Path(regs_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.output_dir / "tuning_history.jsonl"

    def _build_tunables(self, current_cfg: Dict) -> List[Tunable]:
        out = []
        for spec in TUNABLES_DEFAULT:
            cur = current_cfg.get(spec["name"], spec["default"])
            try:
                cur_val = float(cur)
            except (TypeError, ValueError):
                cur_val = float(spec["default"])
            out.append(
                Tunable(
                    name=spec["name"],
                    kind=spec["kind"],
                    current=cur_val,
                    default=float(spec["default"]),
                    min_value=float(spec["min_value"]),
                    max_value=float(spec["max_value"]),
                    max_step=float(spec["max_step"]),
                    rationale=spec["rationale"],
                )
            )
        return out

    @staticmethod
    @staticmethod
    def _trigger_stats(decisions: List[DecisionRecord]) -> Dict[str, Dict[str, int]]:
        stats: Dict[str, Dict[str, int]] = {}
        for d in decisions:
            if d.outcome is None:
                continue
            for t in d.triggers:
                s = stats.setdefault(t, {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "acted": 0, "held": 0})
                if d.decision == "ENTRA":
                    s["acted"] += 1
                    if d.outcome == "tp":
                        s["tp"] += 1
                    else:
                        s["fp"] += 1
                else:
                    s["held"] += 1
                    if d.outcome == "fn":
                        s["fn"] += 1
                    else:
                        s["tn"] += 1
        return stats

    @staticmethod
    def _global_stats(decisions: List[DecisionRecord]) -> Dict[str, Any]:
        acted = [d for d in decisions if d.decision == "ENTRA" and d.outcome]
        held = [d for d in decisions if d.decision != "ENTRA" and d.outcome]
        tp = sum(1 for d in acted if d.outcome == "tp")
        fp = sum(1 for d in acted if d.outcome == "fp")
        fn = sum(1 for d in held if d.outcome == "fn")
        tn = sum(1 for d in held if d.outcome == "tn")
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": precision,
            "recall": recall,
            "n_acted": len(acted),
            "n_held": len(held),
            "n_total": len(acted) + len(held),
        }

    @staticmethod
    def _relevant_triggers_for(param: str) -> List[str]:
        return {
            "trend_rising_threshold": ["ap_rising"],
            "excitation_threshold": ["high_excitation"],
            "corner_rate_threshold": ["high_corner_rate"],
        }.get(param, [])

    def _propose_trigger_threshold(self, t: Tunable, trig_stats: Dict) -> None:
        relevant_keys = self._relevant_triggers_for(t.name)
        tp = sum(trig_stats.get(k, {}).get("tp", 0) for k in relevant_keys)
        fp = sum(trig_stats.get(k, {}).get("fp", 0) for k in relevant_keys)
        fn = sum(trig_stats.get(k, {}).get("fn", 0) for k in relevant_keys)
        n = tp + fp + fn
        t.evidence = {"tp": tp, "fp": fp, "fn": fn, "n": n, "triggers": relevant_keys}

        if n < t.min_sample:
            t.new_value = t.current
            t.delta = 0.0
            return

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0

        if precision < t.target_precision - 0.05 and (tp + fp) >= 5:
            step = min(t.max_step, t.current * 0.15)
            t.new_value = min(t.max_value, t.current + step)
            t.delta = t.new_value - t.current
        elif fn > tp * 1.5 and fn >= 5:
            step = min(t.max_step, t.current * 0.15)
            t.new_value = max(t.min_value, t.current - step)
            t.delta = t.new_value - t.current
        else:
            t.new_value = t.current
            t.delta = 0.0

    def _propose_decision_threshold(self, t: Tunable, gstats: Dict) -> None:
        n = gstats["n_total"]
        t.evidence = {**{k: gstats[k] for k in ("tp", "fp", "fn", "tn", "precision", "recall")}, "n": n}

        if n < t.min_sample:
            t.new_value = t.current
            t.delta = 0.0
            return

        precision = gstats["precision"]
        recall = gstats["recall"]

        # CORREÇÃO: precision == 0 (só FP) deve subir agressivamente
        if (precision < t.target_precision - 0.05) and (gstats["fp"] >= 3):
            step = min(t.max_step, t.current * 0.12)
            t.new_value = min(t.max_value, t.current + step)
            t.delta = t.new_value - t.current
        elif recall < t.target_recall - 0.10 and gstats["fn"] >= 5:
            step = min(t.max_step, t.current * 0.12)
            t.new_value = max(t.min_value, t.current - step)
            t.delta = t.new_value - t.current
        else:
            t.new_value = t.current
            t.delta = 0.0

    @staticmethod
    def _round(t: Tunable) -> None:
        if t.new_value is None:
            return
        if t.kind == "int":
            t.new_value = int(round(t.new_value))
        else:
            t.new_value = round(float(t.new_value), 3)

    def run(self, lookback_days: int = 7, promote: bool = False) -> Dict[str, Any]:
        decisions, _ = EvidenceCollector(
            self.decisions_file, self.regs_dir, lookback_days
        ).collect()
        gstats = self._global_stats(decisions)
        trig_stats = self._trigger_stats(decisions)
        cfg = _load_yaml(self.config_path)
        tunables = self._build_tunables(cfg)
        for t in tunables:
            if t.name.startswith("entra_"):
                self._propose_decision_threshold(t, gstats)
            else:
                self._propose_trigger_threshold(t, trig_stats)
            self._round(t)

        tomorrow_cfg = self._write_tomorrow_config(cfg, tunables)
        report_path = self._write_report(tunables, gstats, trig_stats, lookback_days)
        self._append_history(tunables, gstats)
        # sync proposal for DynamicThresholds (approved=false by default)
        self._write_proposal_json(tunables)
        if promote:
            self._promote(tomorrow_cfg)
        return {
            "global_stats": gstats,
            "changes": [
                {
                    "name": t.name,
                    "from": t.current,
                    "to": t.new_value,
                    "delta": round(t.delta, 4),
                    "evidence": t.evidence,
                }
                for t in tunables
                if abs(t.delta) > 1e-9
            ],
            "tomorrow_config": str(tomorrow_cfg),
            "report": str(report_path),
            "promoted": promote,
            "paper_trade": True,
        }

    def _write_tomorrow_config(self, current_cfg: Dict, tunables: List[Tunable]) -> Path:
        lines = [
            "# glm_config.tomorrow.yaml — PROPOSTA ThresholdTuner",
            f"# Gerada: {datetime.now(timezone.utc).isoformat()}",
            "# Revisar antes de --promote",
            "",
        ]
        keys_in_cfg = set(current_cfg.keys())
        for t in tunables:
            val: Any = int(t.new_value) if t.kind == "int" and t.new_value is not None else t.new_value
            arrow = (
                ""
                if abs(t.delta) < 1e-9
                else f"  # era {t.current} ({'+' if t.delta > 0 else ''}{round(t.delta, 3)})"
            )
            lines.append(f"{t.name}: {val}{arrow}")
            lines.append(f"# {t.rationale}")
            lines.append(
                f"# evidencia: TP={t.evidence.get('tp', 0)} FP={t.evidence.get('fp', 0)} "
                f"FN={t.evidence.get('fn', 0)} n={t.evidence.get('n', 0)}"
            )
            lines.append("")
            keys_in_cfg.discard(t.name)
        for k in sorted(keys_in_cfg):
            v = current_cfg[k]
            if isinstance(v, list):
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
            elif isinstance(v, bool):
                lines.append(f"{k}: {'true' if v else 'false'}")
            else:
                lines.append(f"{k}: {v}")
        out = self.output_dir / "glm_config.tomorrow.yaml"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out

    def _write_proposal_json(self, tunables: List[Tunable]) -> Path:
        """Formato lido por DynamicThresholds se approved=true."""
        by_name = {t.name: t for t in tunables}
        prop = {
            "approved": False,
            "auto_apply": False,
            "paper_trade": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "suggested": {
                "min_score": float(
                    (by_name.get("entra_min_score") or Tunable(
                        "x", "int", 70, 70, 55, 85, 5
                    )).new_value
                    or 70
                ),
                "min_confidence": float(
                    (by_name.get("entra_min_confidence") or Tunable(
                        "x", "float", 0.7, 0.7, 0.5, 0.85, 0.05
                    )).new_value
                    or 0.7
                ),
                "min_triggers": int(
                    (by_name.get("entra_min_triggers") or Tunable(
                        "x", "int", 2, 2, 1, 3, 1
                    )).new_value
                    or 2
                ),
                "min_excitation": float(
                    (by_name.get("excitation_threshold") or Tunable(
                        "x", "float", 0.3, 0.3, 0.15, 0.55, 0.05
                    )).new_value
                    or 0.3
                ),
            },
            "changes": [
                {"name": t.name, "from": t.current, "to": t.new_value, "delta": t.delta}
                for t in tunables
            ],
        }
        daily = self.output_dir.parent / "daily_learning"
        daily.mkdir(parents=True, exist_ok=True)
        out = daily / "threshold_proposal.json"
        out.write_text(json.dumps(prop, ensure_ascii=False, indent=2), encoding="utf-8")
        # also in tuning dir
        (self.output_dir / "threshold_proposal.json").write_text(
            json.dumps(prop, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return out

    def _write_report(
        self,
        tunables: List[Tunable],
        gstats: Dict,
        trig_stats: Dict,
        lookback: int,
    ) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"# TUNING REPORT — {today}",
            f"**Janela:** ultimos {lookback} dias",
            "",
            "## Estatisticas globais",
            f"- Decisoes resolvidas: **{gstats['n_total']}** "
            f"(ENTRA: {gstats['n_acted']}, AGUARDA: {gstats['n_held']})",
            f"- TP: {gstats['tp']} · FP: {gstats['fp']} · FN: {gstats['fn']} · TN: {gstats['tn']}",
            f"- **Precisao: {gstats['precision']:.1%}** | **Recall: {gstats['recall']:.1%}**",
            "",
            "## Propostas",
            "| Parametro | Atual | Proposto | Delta | TP/FP/FN | Status |",
            "|---|---|---|---|---|---|",
        ]
        for t in tunables:
            e = t.evidence
            if abs(t.delta) < 1e-9:
                status = "manter"
            elif t.delta > 0:
                status = "subir"
            else:
                status = "descer"
            delta_s = "—" if abs(t.delta) < 1e-9 else str(round(t.delta, 3))
            lines.append(
                f"| `{t.name}` | {t.current} | **{t.new_value}** | {delta_s} | "
                f"{e.get('tp', 0)}/{e.get('fp', 0)}/{e.get('fn', 0)} "
                f"(n={e.get('n', 0)}) | {status} |"
            )
        lines += ["", "## Rationale"]
        for t in tunables:
            if abs(t.delta) < 1e-9:
                lines.append(f"- **{t.name}**: mantido ({t.current})")
            else:
                arrow = "subiu" if t.delta > 0 else "desceu"
                lines.append(
                    f"- **{t.name}**: {arrow} {t.current} → **{t.new_value}** — {t.rationale}"
                )
        if gstats["n_total"] < 30:
            lines += [
                "",
                "## Aviso",
                f"Amostra baixa ({gstats['n_total']}). Mudancas conservadoras.",
            ]
        lines += ["", "---", "paper_trade=true · promote apenas com revisao humana"]
        path = self.output_dir / f"tuning_report_{today}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _append_history(self, tunables: List[Tunable], gstats: Dict) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "global": {
                k: gstats[k]
                for k in ("tp", "fp", "fn", "tn", "precision", "recall", "n_total")
            },
            "changes": [
                {
                    "name": t.name,
                    "from": t.current,
                    "to": t.new_value,
                    "delta": round(t.delta, 4) if t.delta else 0.0,
                }
                for t in tunables
            ],
        }
        with self.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _promote(self, tomorrow_cfg: Path) -> None:
        if not tomorrow_cfg.exists():
            return
        if self.config_path.exists():
            backup = self.config_path.with_suffix(
                f".bak.{datetime.now().strftime('%Y%m%d')}.yaml"
            )
            shutil.copy2(self.config_path, backup)
            logger.info("Backup: %s", backup)
        data = _load_yaml(tomorrow_cfg)
        # merge: prefer numeric tunables from tomorrow file text
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(_dump_yaml(data), encoding="utf-8")
        # mark proposal approved for DynamicThresholds
        prop_path = self.output_dir.parent / "daily_learning" / "threshold_proposal.json"
        if prop_path.exists():
            try:
                prop = json.loads(prop_path.read_text(encoding="utf-8"))
                prop["approved"] = True
                prop_path.write_text(
                    json.dumps(prop, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
        logger.info("Config promovida: %s", self.config_path)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    ap = argparse.ArgumentParser(description="ThresholdTuner AURA+GLM")
    ap.add_argument(
        "--config",
        default=str(root / "engine" / "agents" / "glm_config.yaml"),
        type=Path,
    )
    ap.add_argument(
        "--decisions",
        default=str(root / "engine" / "data" / "glm_decisions.jsonl"),
        type=Path,
    )
    ap.add_argument(
        "--regs-dir",
        default=str(root / "bridge" / "regs"),
        type=Path,
    )
    ap.add_argument(
        "--out",
        default=str(root / "engine" / "data" / "tuning"),
        type=Path,
    )
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument(
        "--promote",
        action="store_true",
        help="Aplica proposta (backup + sobrescreve config ativa)",
    )
    args = ap.parse_args()
    tuner = ThresholdTuner(
        config_path=args.config,
        decisions_file=args.decisions,
        regs_dir=args.regs_dir,
        output_dir=args.out,
    )
    result = tuner.run(lookback_days=args.days, promote=args.promote)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

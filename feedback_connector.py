#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FeedbackConnector — fecha o loop de aprendizado AURA + GLM V2.
Lê: bridge/regs/REG-*.md + data/glm_decisions.jsonl
Grava: MemoryStore + data/daily_learning/YYYY-MM-DD.md
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FEEDBACK] %(levelname)s: %(message)s",
)
logger = logging.getLogger("aura.feedback")

HORIZON = {"35_ht": 8, "85_ft": 10, "out": 10}


@dataclass
class RegSnapshot:
    reg_id: str
    fixture_id: Optional[str] = None
    home: Optional[str] = None
    away: Optional[str] = None
    league: Optional[str] = None
    minute: Optional[int] = None
    extra: int = 0
    corners_home: Optional[int] = None
    corners_away: Optional[int] = None
    corner_minutes: List[int] = field(default_factory=list)
    pos_canto_minuto: Optional[int] = None
    pos_acerto: Optional[str] = None
    pos_final_corners: Optional[Tuple[int, int]] = None


class RegParser:
    RE_ID = re.compile(r"^###\s+(REG-[A-Za-z0-9_-]+)", re.M)
    RE_FIXTURE = re.compile(r"- Fixture:\s*(\S+|N/D)\s*\|\s*Janela:\s*(\w+)")
    RE_TEAMS = re.compile(r"^-\s+(.+?)\s*\|\s*(.+?)\s*[×x]\s*(.+?)$", re.M)
    RE_MINUTE = re.compile(r"- Minuto:\s*(\d+)'?\s*(?:\(\+(\d+)\))?")
    RE_CORNERS = re.compile(
        r"\|\s*Escanteios\s*\|\s*(\d+|None)\s*\|\s*(\d+|None)\s*\|", re.I
    )
    RE_EVENT = re.compile(r"^-\s*(\d+)'?\s*[·•]\s*(\w+)", re.M)
    RE_POS_CANTO = re.compile(r"- Minuto real canto:\s*(\d+)")
    RE_POS_ACERTO = re.compile(r"- Acerto/Erro:\s*(Acerto|Erro)", re.I)
    RE_POS_FINAL = re.compile(
        r"- Cantos finais[^:]*:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)"
    )

    @staticmethod
    def _normalize_team_name(value: str) -> str:
        name = value.strip()
        if re.match(r"^Time\s+[A-Za-z0-9]", name):
            return "Team" + name[4:]
        return name

    @classmethod
    def parse_file(cls, path: Path) -> Optional[RegSnapshot]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        m = cls.RE_ID.search(text)
        if not m:
            # fallback: use filename as id
            snap = RegSnapshot(reg_id=path.stem)
        else:
            snap = RegSnapshot(reg_id=m.group(1))
        if (fm := cls.RE_FIXTURE.search(text)) and fm.group(1) != "N/D":
            snap.fixture_id = fm.group(1)
        if tm := cls.RE_TEAMS.search(text):
            snap.league = tm.group(1).strip()
            snap.home = cls._normalize_team_name(tm.group(2))
            snap.away = cls._normalize_team_name(tm.group(3))
        if mm := cls.RE_MINUTE.search(text):
            snap.minute = int(mm.group(1))
            snap.extra = int(mm.group(2) or 0)
        if cm := cls.RE_CORNERS.search(text):
            snap.corners_home = None if cm.group(1) == "None" else int(cm.group(1))
            snap.corners_away = None if cm.group(2) == "None" else int(cm.group(2))
        # Eventos cumulativos — sem sombra de variável
        events = cls.RE_EVENT.findall(text)
        snap.corner_minutes = [int(match[0]) for match in events] if events else []
        if p := cls.RE_POS_CANTO.search(text):
            snap.pos_canto_minuto = int(p.group(1))
        if p := cls.RE_POS_ACERTO.search(text):
            snap.pos_acerto = p.group(1).lower()
        if p := cls.RE_POS_FINAL.search(text):
            snap.pos_final_corners = (int(p.group(1)), int(p.group(2)))
        return snap


@dataclass
class DecisionRecord:
    ts: str
    fixture_id: Optional[str]
    home: Optional[str]
    away: Optional[str]
    minute: Optional[int]
    window: str
    decision: str
    confidence: float
    triggers: List[str]
    kills: List[str]
    outcome: Optional[str] = None
    corner_in_horizon: Optional[bool] = None


class OutcomeResolver:
    def __init__(self, decisions: List[DecisionRecord], snapshots: List[RegSnapshot]):
        self.decisions = decisions
        self.by_fixture: Dict[str, List[RegSnapshot]] = defaultdict(list)
        for s in snapshots:
            if s.fixture_id and s.minute is not None:
                self.by_fixture[s.fixture_id].append(s)
        for fid in self.by_fixture:
            self.by_fixture[fid].sort(key=lambda s: s.minute or 0)

    def _final_state(self, fixture_id: str) -> Optional[RegSnapshot]:
        regs = self.by_fixture.get(fixture_id)
        return regs[-1] if regs else None

    def _corner_after(
        self, fixture_id: str, from_min: int, horizon: int
    ) -> Optional[bool]:
        final = self._final_state(fixture_id)
        if final is None:
            return None
        corners = list(final.corner_minutes or [])
        if final.pos_canto_minuto is not None:
            corners = sorted(set(corners + [final.pos_canto_minuto]))
        if not corners:
            return False
        return any(from_min < c <= from_min + horizon for c in corners)

    def resolve_all(self) -> List[DecisionRecord]:
        for d in self.decisions:
            if d.fixture_id is None or d.minute is None:
                continue
            horizon = HORIZON.get(d.window, 10)
            hit = self._corner_after(d.fixture_id, int(d.minute), horizon)
            if hit is None:
                continue
            d.corner_in_horizon = hit
            if d.decision == "ENTRA":
                d.outcome = "tp" if hit else "fp"
            else:
                d.outcome = "tn" if not hit else "fn"
        return self.decisions


class FeedbackConnector:
    STATE_VERSION = 1

    def __init__(
        self,
        regs_dir: Path,
        decisions_file: Path,
        memory,
        report_dir: Path = Path("engine/data/daily_learning"),
        state_file: Optional[Path] = None,
    ):
        self.regs_dir = Path(regs_dir)
        self.decisions_file = Path(decisions_file)
        self.memory = memory
        self.report_dir = Path(report_dir)
        self.state_file = state_file or (
            Path("engine/data") / "feedback_state.json"
        )
        self.state: Dict = self._load_state()

    def _load_state(self) -> Dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"version": self.STATE_VERSION, "resolved": [], "pending": []}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def load_decisions(self) -> List[DecisionRecord]:
        if not self.decisions_file.exists():
            return []
        out: List[DecisionRecord] = []
        for line in self.decisions_file.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
                out.append(
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
        return out

    def load_snapshots(self) -> List[RegSnapshot]:
        snaps: List[RegSnapshot] = []
        if not self.regs_dir.exists():
            return snaps
        for p in sorted(self.regs_dir.glob("REG-*.md")):
            s = RegParser.parse_file(p)
            if s:
                snaps.append(s)
        return snaps

    def run_cycle(self) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        decisions = [d for d in self.load_decisions() if d.ts.startswith(today)]
        snapshots = self.load_snapshots()  # load uma vez só

        resolved = OutcomeResolver(decisions, snapshots).resolve_all()

        stats = self._feed_memory(resolved, today)
        report = self.generate_report(resolved, today)

        new_resolved = [d.ts for d in resolved if d.outcome]
        self.state["resolved"] = list(set(self.state.get("resolved", []) + new_resolved))[-5000:]
        self._save_state()
        return {"stats": stats, "report_path": str(report)}

    def _feed_memory(self, decisions: List[DecisionRecord], today: str) -> Dict:
        stats: Dict[str, int] = defaultdict(int)
        seen_enter: set = set()
        for d in decisions:
            if d.outcome is None:
                continue
            stats[d.outcome] += 1
            key = (d.fixture_id, d.window)
            if d.decision == "ENTRA":
                if key in seen_enter:
                    stats["deduped"] += 1
                    continue
                seen_enter.add(key)
                self.memory.record_outcome(
                    fixture_id=d.fixture_id or "?",
                    triggers=d.triggers,
                    confidence=d.confidence,
                    was_correct=(d.outcome == "tp"),
                )
            elif d.outcome == "fn":
                self._append_missed(d, today)
        stats["total_resolved"] = sum(
            stats.get(k, 0) for k in ("tp", "fp", "tn", "fn")
        )
        return dict(stats)

    def _append_missed(self, d: DecisionRecord, today: str) -> None:
        path = self.report_dir / f"missed_{today}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": d.ts,
                        "fixture_id": d.fixture_id,
                        "match": f"{d.home} x {d.away}",
                        "minute": d.minute,
                        "window": d.window,
                        "confidence": d.confidence,
                        "kills": d.kills,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def generate_report(
        self, decisions: List[DecisionRecord], today: str
    ) -> Path:
        res = [d for d in decisions if d.outcome]
        by = lambda o: sum(1 for d in res if d.outcome == o)
        tp, fp, tn, fn = by("tp"), by("fp"), by("tn"), by("fn")
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        trig_stats: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"tp": 0, "fp": 0}
        )
        for d in res:
            if d.decision == "ENTRA" and d.outcome in ("tp", "fp"):
                for t in d.triggers:
                    trig_stats[t][d.outcome] += 1
        calib = {}
        best = []
        try:
            calib = self.memory.calibration_report()
            best = self.memory.best_patterns(min_n=5, min_rate=0.55)
        except Exception as e:
            logger.warning("memory report: %s", e)

        lines = [
            f"# APRENDIZADO DO DIA — {today}",
            "",
            f"**Decisoes resolvidas:** {len(res)} | "
            f"TP: {tp} · FP: {fp} · TN: {tn} · FN: {fn}",
            f"**Precisao (ENTRA):** {precision:.1%} | **Recall:** {recall:.1%}",
            "",
            "## Gatilhos por resultado (ENTRA)",
            "| Gatilho | Acertos | Erros | Taxa |",
            "|---|---|---|---|",
        ]
        for t, s in sorted(
            trig_stats.items(), key=lambda kv: -(kv[1]["tp"] + kv[1]["fp"])
        ):
            tot = s["tp"] + s["fp"]
            if tot:
                lines.append(
                    f"| {t} | {s['tp']} | {s['fp']} | {s['tp']/tot:.0%} |"
                )
        lines += [
            "",
            "## Calibracao (confianca declarada vs. real)",
            "| Banda | Declarada | Real | N |",
            "|---|---|---|---|",
        ]
        for band, v in sorted(calib.items()):
            lines.append(
                f"| {band} | {v['declared']:.0%} | {v['actual']:.0%} | {v['n']} |"
            )
        if best:
            lines += ["", "## Padroes validados"]
            for p in best:
                lines.append(
                    f"- `{p['triggers']}` → {p['rate']:.0%} (n={p['n']})"
                )
        if (tp + fp) >= 10:
            if precision < 0.5:
                sug = "BAIXAR threshold de ENTRA (muitos FP)"
            elif precision >= 0.65:
                sug = "MANTER/Subir levemente (precisao saudavel)"
            else:
                sug = "MANTER threshold (precisao marginal)"
            lines += ["", f"## Sugestao de ajuste: **{sug}**"]
        lines += [
            "",
            "---",
            "paper_trade=true · execution_allowed=false",
        ]
        self.report_dir.mkdir(parents=True, exist_ok=True)
        out = self.report_dir / f"{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        logger.info(
            "Relatorio: %s (TP:%s FP:%s FN:%s prec:%.0f%%)",
            out,
            tp,
            fp,
            fn,
            precision * 100,
        )
        return out

    def watch(self, interval_sec: int = 300) -> None:
        logger.info("Daemon a cada %ss em %s", interval_sec, self.regs_dir)
        while True:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error("cycle falhou: %s", e)
            time.sleep(interval_sec)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    try:
        from agents.memory_store import MemoryStore
    except ImportError:
        from engine.agents.memory_store import MemoryStore

    ap = argparse.ArgumentParser(description="FeedbackConnector AURA+GLM")
    ap.add_argument(
        "--regs-dir",
        default=str(root / "bridge" / "regs"),
        type=Path,
    )
    ap.add_argument(
        "--decisions",
        default=str(root / "engine" / "data" / "glm_decisions.jsonl"),
        type=Path,
    )
    ap.add_argument(
        "--memory",
        default=str(root / "engine" / "data" / "glm_memory.json"),
        type=Path,
    )
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    args = ap.parse_args()

    conn = FeedbackConnector(
        regs_dir=args.regs_dir,
        decisions_file=args.decisions,
        memory=MemoryStore(Path(args.memory)),
        report_dir=root / "engine" / "data" / "daily_learning",
        state_file=root / "engine" / "data" / "feedback_state.json",
    )
    if args.report:
        decisions = conn.load_decisions()
        snaps = conn.load_snapshots()
        resolved = OutcomeResolver(decisions, snaps).resolve_all()
        print(conn.generate_report(resolved, datetime.now().strftime("%Y-%m-%d")))
    elif args.watch:
        conn.watch(args.interval)
    else:
        print(json.dumps(conn.run_cycle(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

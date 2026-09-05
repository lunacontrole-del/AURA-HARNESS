"""Analisa logs de aprendizagem do Live Window Learner (paper-only)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def load_entries(ledger_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not ledger_path.exists():
        return rows
    with open(ledger_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def analyze_learning_logs(base_dir: str | Path | None = None) -> Dict[str, Any]:
    root = Path(base_dir or Path(__file__).resolve().parents[1])
    ledger = root / "logs" / "learning" / "paper_corner_windows.jsonl"
    samples = root / "logs" / "learning" / "learn_samples.jsonl"
    patterns = root / "logs" / "learning" / "patterns_summary.json"

    entries = load_entries(ledger)
    enters = [e for e in entries if e.get("decision") == "ENTER"]
    closed = [e for e in enters if e.get("status") in ("GREEN", "RED")]
    opens = [e for e in enters if e.get("status") == "OPEN"]
    voids = [e for e in entries if e.get("decision") == "SKIP" or e.get("status") == "VOID"]

    greens = [e for e in closed if e.get("status") == "GREEN"]
    reds = [e for e in closed if e.get("status") == "RED"]
    n = len(closed)
    acc = (len(greens) / n) if n else None

    by_window: Dict[str, Dict[str, int]] = defaultdict(lambda: {"GREEN": 0, "RED": 0, "OPEN": 0, "ENTER": 0})
    for e in enters:
        w = str(e.get("window") or "?")
        by_window[w]["ENTER"] += 1
        st = e.get("status")
        if st in ("GREEN", "RED", "OPEN"):
            by_window[w][st] += 1

    # scores médios nas entradas
    scores = []
    for e in enters:
        a = e.get("analysis") or {}
        if a.get("score") is not None:
            try:
                scores.append(float(a["score"]))
            except (TypeError, ValueError):
                pass

    report = {
        "ok": True,
        "paper_only": True,
        "real_stake": False,
        "ledger_path": str(ledger),
        "ledger_exists": ledger.exists(),
        "total_records": len(entries),
        "enters": len(enters),
        "skips_or_void": len(voids),
        "open": len(opens),
        "closed": n,
        "greens": len(greens),
        "reds": len(reds),
        "accuracy": round(acc, 4) if acc is not None else None,
        "by_window": dict(by_window),
        "avg_entry_score": round(sum(scores) / len(scores), 4) if scores else None,
        "samples_path": str(samples),
        "samples_exist": samples.exists(),
        "patterns_path": str(patterns),
        "message": (
            "Sem entradas fechadas ainda — deixe partidas rodarem nas janelas 35' e 85'."
            if n == 0
            else f"Acurácia paper {acc:.1%} em {n} entradas fechadas."
        ),
    }
    # persiste resumo legível
    out = root / "logs" / "learning" / "analysis_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_path"] = str(out)
    return report


if __name__ == "__main__":
    print(json.dumps(analyze_learning_logs(), indent=2, ensure_ascii=False))

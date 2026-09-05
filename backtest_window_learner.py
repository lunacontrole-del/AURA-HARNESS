"""
Backtesting histórico das janelas 35'/85' (paper-only, sem look-ahead abusivo).
Simula telemetria minuto a minuto a partir de séries sintéticas calibradas
ou de learn_samples / ledger quando existirem outcomes.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from analyze_learning_logs import analyze_learning_logs


def _synth_matches(n: int = 40, seed: int = 42) -> List[Dict[str, Any]]:
    """Gera partidas sintéticas com cantos ao longo do tempo."""
    rng = random.Random(seed)
    matches = []
    for i in range(n):
        fid = f"bt-{i:04d}"
        # ritmo de cantos: baixo / médio / alto
        pace = rng.choice([0.08, 0.12, 0.18, 0.22])
        corners_timeline = []
        total = 0
        for m in range(1, 96):
            if rng.random() < pace * (1.1 if m >= 35 else 0.9) * (1.15 if m >= 85 else 1.0):
                total += 1
            corners_timeline.append(total)
        matches.append({
            "fixture_id": fid,
            "home": f"Home{i}",
            "away": f"Away{i}",
            "pace": pace,
            "corners_timeline": corners_timeline,
            "danger_base": rng.uniform(10, 30),
        })
    return matches


def run_window_backtest(
    base_dir: Optional[str] = None,
    n_matches: int = 40,
    min_confidence: float = 0.42,
    seed: int = 42,
) -> Dict[str, Any]:
    root = Path(base_dir or Path(__file__).resolve().parents[1])
    import sys
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    import importlib.util
    _lp = root / "core" / "agent" / "live_window_learner.py"
    _spec = importlib.util.spec_from_file_location("live_window_learner", _lp)
    _mod = importlib.util.module_from_spec(_spec)
    import sys as _sys
    _sys.modules["live_window_learner"] = _mod
    _spec.loader.exec_module(_mod)
    LiveWindowLearner = _mod.LiveWindowLearner

    # Learner isolado em pasta de backtest (não mistura com live)
    bt_root = root / "logs" / "learning" / "backtest_run"
    bt_root.mkdir(parents=True, exist_ok=True)
    # limpa ledger anterior desta pasta
    ledger = bt_root / "logs" / "learning" / "paper_corner_windows.jsonl"
    if ledger.exists():
        ledger.unlink()

    learner = LiveWindowLearner(base_dir=str(bt_root), min_confidence=min_confidence)
    matches = _synth_matches(n_matches, seed=seed)

    for match in matches:
        timeline = match["corners_timeline"]
        # Só minutos relevantes (gatilhos + resolução) — backtest rápido
        for minute in [34, 35, 36, 38, 42, 48, 51, 84, 85, 86, 88, 92, 96]:
            corners = timeline[minute - 1]
            # distribui home/away de forma simples
            ch = corners // 2
            ca = corners - ch
            payload = {
                "fixtureId": match["fixture_id"],
                "home": match["home"],
                "away": match["away"],
                "minute": minute,
                "stats": {
                    "corners": {"home": ch, "away": ca},
                    "dangerous": {
                        "home": match["danger_base"] * (minute / 45),
                        "away": match["danger_base"] * 0.9 * (minute / 45),
                    },
                },
                "odds_velocity": 0.01,
            }
            learner.on_telemetry(payload)

    summary = learner.build_learning_dataset()
    analysis = analyze_learning_logs(str(bt_root))

    report = {
        "ok": True,
        "paper_only": True,
        "real_stake": False,
        "n_matches": n_matches,
        "min_confidence": min_confidence,
        "learner_summary": summary,
        "log_analysis": analysis,
        "note": (
            "Backtest sintético calibrado por ritmo de cantos. "
            "Substitua por outcomes reais do ledger quando houver volume."
        ),
    }
    out = root / "logs" / "learning" / "backtest_windows_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_path"] = str(out)
    return report


if __name__ == "__main__":
    print(json.dumps(run_window_backtest(), indent=2, ensure_ascii=False))

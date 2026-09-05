from __future__ import annotations

# backtest_engine.py — Backtest honesto com métricas (sem look-ahead)
"""
Uso:
  python backtest_engine.py --db aura_quant_x.db
  python backtest_engine.py --jsonl signals.jsonl

Espera registros com: prob, signal, outcome (0/1), odds, stake_pct (opcional)
Se outcome ausente, só reporta estatísticas descritivas dos sinais.
"""
import argparse
import json
import sqlite3
from pathlib import Path
from typing import List, Dict, Any

from metrics import summarize_signals, brier_score

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "aura_quant_x.db")


def load_from_sqlite(db_path: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # tabela de outcomes (criada pelo feedback)
    try:
        cur.execute(
            """SELECT signal, corner_prob, goal_prob, stake, outcome, odds, pnl
               FROM signal_outcomes WHERE outcome IS NOT NULL"""
        )
        rows = []
        for r in cur.fetchall():
            sig = r["signal"] or "HOLD"
            prob = float(r["corner_prob"] or 0) if "CORNER" in sig else float(r["goal_prob"] or 0)
            rows.append({
                "signal": sig,
                "prob": prob,
                "outcome": int(r["outcome"]),
                "stake": float(r["stake"] or 1),
                "odds": float(r["odds"] or 1.85),
                "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            })
        conn.close()
        return rows
    except sqlite3.OperationalError:
        conn.close()
        return []


def load_from_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if o.get("outcome") is None:
                continue
            sig = o.get("signal", "HOLD")
            prob = float(o.get("prob") or o.get("corner_prob") or 0)
            rows.append({
                "signal": sig,
                "prob": prob,
                "outcome": int(o["outcome"]),
                "stake": float(o.get("stake") or o.get("stake_pct") or 1),
                "odds": float(o.get("odds") or 1.85),
                "pnl": o.get("pnl"),
            })
    return rows


def fill_pnl(rows: List[Dict[str, Any]], bankroll: float = 1000.0) -> List[Dict[str, Any]]:
    """Se pnl ausente, simula: stake = stake% da banca; win = stake*(odds-1); loss = -stake."""
    out = []
    for r in rows:
        r = dict(r)
        if r.get("pnl") is not None:
            out.append(r)
            continue
        stake_pct = float(r.get("stake") or 1)
        # se stake parece % (ex 1.5), trata como %
        if stake_pct <= 10:
            stake_amt = bankroll * (stake_pct / 100.0)
        else:
            stake_amt = stake_pct
        odds = float(r.get("odds") or 1.85)
        if int(r["outcome"]) == 1:
            r["pnl"] = stake_amt * (odds - 1.0)
        else:
            r["pnl"] = -stake_amt
        r["stake"] = stake_amt
        out.append(r)
    return out


def run_backtest(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {
            "error": "Sem dados com outcome. Registre resultados via POST /api/outcome",
            "hint": "Paper trading primeiro: grave cada BUY e depois o resultado 0/1",
        }
    rows = fill_pnl(rows)
    summary = summarize_signals(rows)

    # Baseline: sempre BUY_CORNER com prob=0.5
    baseline_rows = [
        {**r, "prob": 0.5, "signal": "BUY_CORNER"} for r in rows if r.get("signal") != "HOLD"
    ]
    if baseline_rows:
        # mesma outcomes — só muda “modelo”
        b_probs = [0.5] * len(baseline_rows)
        b_out = [int(r["outcome"]) for r in baseline_rows]
        summary["baseline_brier_always_0.5"] = round(brier_score(b_probs, b_out), 4)

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--jsonl", default="")
    args = ap.parse_args()

    rows: List[Dict[str, Any]] = []
    if args.jsonl and Path(args.jsonl).exists():
        rows = load_from_jsonl(args.jsonl)
        print(f"Carregados {len(rows)} de {args.jsonl}")
    elif Path(args.db).exists():
        rows = load_from_sqlite(args.db)
        print(f"Carregados {len(rows)} de {args.db}")
    else:
        print("Nenhuma fonte de dados. Use --db ou --jsonl")
        return

    result = run_backtest(rows)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

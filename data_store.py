# data_store.py — Persistência de sinais + outcomes para backtest
from __future__ import annotations
import atexit
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# O instalador pode ser chamado por duplo clique, atalho ou PowerShell com
# diretório de trabalho diferente. O banco deve permanecer junto do Engine.
DB_PATH = str(Path(__file__).resolve().parent / "aura_quant_x.db")


def get_conn(path: str = DB_PATH):
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        # WAL já é o modo canônico do AURA; os demais PRAGMAs limitam a
        # contenção e fornecem um cache por conexão sem criar outro banco.
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("PRAGMA cache_size=-32000")  # P1 unified 32MB
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            pass
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=400")  # P1
        conn.execute("PRAGMA trusted_schema=OFF")  # P1
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn


# --- V23 OTIMIZAÇÃO 1: Pool de conexões thread-local ---
_thread_local = threading.local()


class SQLiteBatchWriter:
    """Serializa escritas não críticas e agrupa a fila em intervalos curtos."""
    def __init__(self, path: str, interval_sec: float = 3.0, max_queue: int = 2048):
        self.path = path
        self.interval_sec = max(0.25, float(interval_sec))
        self.max_queue = max(1, int(max_queue))
        self._queue: list[tuple[str, tuple[Any, ...]]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aura-sqlite-batch-writer", daemon=True)
        self._thread.start()

    def enqueue(self, sql: str, params: tuple[Any, ...]) -> bool:
        with self._lock:
            if len(self._queue) >= self.max_queue:
                return False
            self._queue.append((sql, params))
        self._wake.set()
        return True

    def _drain(self) -> list[tuple[str, tuple[Any, ...]]]:
        with self._lock:
            batch = self._queue[:]
            self._queue.clear()
            return batch

    def flush(self) -> int:
        batch = self._drain()
        if not batch:
            return 0
        conn = None
        try:
            conn = get_conn(self.path)
            grouped: dict[str, list[tuple[Any, ...]]] = {}
            for sql, params in batch:
                grouped.setdefault(sql, []).append(params)
            cur = conn.cursor()
            for sql, rows in grouped.items():
                cur.executemany(sql, rows)
            conn.commit()
            return len(batch)
        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            with self._lock:
                available = self.max_queue - len(self._queue)
                self._queue = batch[-available:] + self._queue if available else self._queue
            return 0
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _run(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self._wake.clear()
            self.flush()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self.flush()


_BATCH_WRITERS: dict[str, SQLiteBatchWriter] = {}
_BATCH_WRITERS_LOCK = threading.Lock()


def get_batch_writer(path: str = DB_PATH) -> SQLiteBatchWriter:
    with _BATCH_WRITERS_LOCK:
        writer = _BATCH_WRITERS.get(path)
        if writer is None:
            writer = SQLiteBatchWriter(path)
            _BATCH_WRITERS[path] = writer
        return writer


def _close_batch_writers() -> None:
    with _BATCH_WRITERS_LOCK:
        writers = list(_BATCH_WRITERS.values())
    for writer in writers:
        writer.close()


atexit.register(_close_batch_writers)


def get_thread_safe_conn(path: str = DB_PATH):
    """Retorna uma conexão SQLite reutilizada por thread (evita open/close por evento).

    V24: valida se a conexão cacheada ainda está viva antes de reusar — call sites
    antigos que faziam _maybe_close(conn) deixavam um objeto Python morto no thread-local.
    """
    cached = getattr(_thread_local, "conn", None)
    cached_path = getattr(_thread_local, "path", None)
    if cached is not None and cached_path == path:
        try:
            cached.execute("SELECT 1")
            return cached
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            try:
                cached.close()
            except Exception:
                pass
            cached = None
    if cached is not None and cached_path != path:
        try:
            cached.close()
        except Exception:
            pass
    _thread_local.conn = get_conn(path)
    _thread_local.path = path
    return _thread_local.conn


_SCHEMA_READY: dict = {}
_SCHEMA_LOCK = __import__("threading").Lock()


def ensure_schema_once(path: str = DB_PATH) -> None:
    """init_schema apenas uma vez por (processo, path)."""
    if _SCHEMA_READY.get(path):
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY.get(path):
            return
        init_schema(path)
        _SCHEMA_READY[path] = True


def hot_db(path: str = DB_PATH):
    """Alias P1 Extreme: schema once + thread-local hot connection."""
    return get_pooled_conn(path)


def get_pooled_conn(path: str = DB_PATH):
    """Schema once + conexao thread-local."""
    ensure_schema_once(path)
    return get_thread_safe_conn(path)




def _maybe_close(conn) -> None:
    """V24: never close thread-local pooled connections. Ad-hoc get_conn() still closes."""
    pooled = getattr(_thread_local, "conn", None)
    if pooled is not None and conn is pooled:
        return  # keep alive for the thread
    try:
        _maybe_close(conn)
    except Exception:
        pass

def upgrade_db_schema_v12_6(cursor) -> None:
    """Adiciona colunas de mercado (Weight of Money) na v12.6.0."""
    for ddl in (
        "ALTER TABLE logs_telemetria ADD COLUMN asian_corner_line REAL DEFAULT 0.0",
        "ALTER TABLE logs_telemetria ADD COLUMN asian_corner_odds REAL DEFAULT 0.0",
        "ALTER TABLE logs_telemetria ADD COLUMN odds_velocity REAL DEFAULT 0.0",
    ):
        try:
            cursor.execute(ddl)
        except Exception:
            pass  # coluna já existe
    pass  # schema upgrade silent


def init_schema(path: str = DB_PATH):
    conn = get_conn(path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS logs_telemetria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id TEXT,
            timestamp REAL,
            signal TEXT,
            corner_prob REAL,
            goal_prob REAL,
            stake REAL,
            payload_json TEXT
        )
    """)
    upgrade_db_schema_v12_6(c)
    c.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            fixture_id TEXT,
            timestamp REAL,
            signal TEXT,
            corner_prob REAL,
            goal_prob REAL,
            stake REAL,
            odds REAL,
            outcome INTEGER,
            pnl REAL,
            notes TEXT,
            UNIQUE(fixture_id, signal, timestamp)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id TEXT,
            created_at REAL,
            signal TEXT,
            prob REAL,
            odds REAL,
            stake_pct REAL,
            stake_amount REAL,
            outcome INTEGER,
            pnl REAL,
            status TEXT DEFAULT 'open'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS risk_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            min_prob_corner REAL,
            min_prob_goal REAL,
            max_stake_pct REAL,
            sample_n INTEGER,
            brier REAL,
            roi REAL,
            notes TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS feature_quality_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id TEXT NOT NULL,
            captured_at REAL,
            received_at REAL NOT NULL,
            status_counts_json TEXT NOT NULL,
            missing_fields_json TEXT NOT NULL,
            invalid_fields_json TEXT NOT NULL,
            critical_missing_fields_json TEXT NOT NULL,
            legacy_default_count INTEGER NOT NULL DEFAULT 0,
            schema_version TEXT,
            created_at REAL NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            fixture_id TEXT NOT NULL,
            source TEXT,
            source_ts REAL,
            received_ts REAL NOT NULL,
            sequence_no INTEGER,
            schema_version TEXT,
            raw_hash TEXT,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(event_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS decision_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL,
            event_id TEXT,
            fixture_id TEXT NOT NULL,
            ts REAL NOT NULL,
            signal TEXT NOT NULL,
            decision TEXT,
            corner_prob REAL,
            goal_prob REAL,
            kelly REAL,
            approved INTEGER,
            integrity_status TEXT,
            integrity_json TEXT,
            flags_json TEXT,
            feature_quality_json TEXT,
            model_version TEXT,
            policy TEXT,
            reason TEXT,
            analysis_json TEXT,
            created_at REAL NOT NULL,
            UNIQUE(decision_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS data_quality_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flag_id TEXT NOT NULL,
            fixture_id TEXT,
            decision_id TEXT,
            event_id TEXT,
            flag_code TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT,
            details_json TEXT,
            ts REAL NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(flag_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS model_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT NOT NULL,
            model_name TEXT,
            schema_version TEXT,
            checksum TEXT,
            metrics_json TEXT,
            notes TEXT,
            created_at REAL NOT NULL,
            UNIQUE(model_version)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label_id TEXT NOT NULL,
            fixture_id TEXT NOT NULL,
            target_name TEXT NOT NULL,
            product TEXT NOT NULL,
            horizon_sec INTEGER NOT NULL,
            window_start_ts REAL,
            window_end_ts REAL,
            match_seconds_start REAL,
            label INTEGER,
            censored INTEGER NOT NULL DEFAULT 0,
            censor_reason TEXT,
            window_complete INTEGER NOT NULL DEFAULT 0,
            label_version TEXT NOT NULL,
            source_event_id TEXT,
            details_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(label_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_labels_fixture ON labels(fixture_id, horizon_sec, window_start_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_fixture ON raw_events(fixture_id, received_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_decision_logs_fixture ON decision_logs(fixture_id, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_dq_flags_fixture ON data_quality_flags(fixture_id, ts)")
    # --- V23 OTIMIZAÇÃO 2: Índices compostos para backtest / results_agent ---
    c.execute("CREATE INDEX IF NOT EXISTS idx_decision_fixture_ts ON decision_logs(fixture_id, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_fixture_ts ON raw_events(fixture_id, received_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_fixture_status ON paper_trades(fixture_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON logs_telemetria(timestamp)")
    conn.commit()
    _maybe_close(conn)


def log_signal(
    fixture_id: str,
    signal: str,
    corner_prob: float,
    goal_prob: float,
    stake: float,
    payload: Optional[Dict[str, Any]] = None,
    path: str = DB_PATH,
    asian_corner_line: float = 0.0,
    asian_corner_odds: float = 0.0,
    odds_velocity: float = 0.0,
) -> int:
    conn = get_pooled_conn(path)
    c = conn.cursor()
    c.execute(
        """INSERT INTO logs_telemetria
           (fixture_id, timestamp, signal, corner_prob, goal_prob, stake, payload_json,
            asian_corner_line, asian_corner_odds, odds_velocity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fixture_id,
            time.time(),
            signal,
            corner_prob,
            goal_prob,
            stake,
            json.dumps(payload or {}),
            asian_corner_line,
            asian_corner_odds,
            odds_velocity,
        ),
    )
    sid = c.lastrowid
    conn.commit()
    _maybe_close(conn)
    return int(sid)


def open_paper_trade(
    fixture_id: str,
    signal: str,
    prob: float,
    odds: float,
    stake_pct: float,
    bankroll: float = 1000.0,
    path: str = DB_PATH,
) -> int:
    init_schema(path)
    amount = bankroll * (stake_pct / 100.0)
    conn = get_conn(path)
    c = conn.cursor()
    c.execute(
        """INSERT INTO paper_trades
           (fixture_id, created_at, signal, prob, odds, stake_pct, stake_amount, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
        (fixture_id, time.time(), signal, prob, odds, stake_pct, amount),
    )
    tid = c.lastrowid
    conn.commit()
    _maybe_close(conn)
    return int(tid)


def resolve_paper_trade(
    trade_id: int,
    outcome: int,
    path: str = DB_PATH,
) -> Dict[str, Any]:
    """outcome: 1 = green, 0 = red."""
    conn = get_pooled_conn(path)
    c = conn.cursor()
    c.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,))
    row = c.fetchone()
    if not row:
        _maybe_close(conn)
        return {"error": "trade not found"}
    cols = [d[0] for d in c.description]
    trade = dict(zip(cols, row))
    if trade.get("status") == "closed":
        _maybe_close(conn)
        return {"error": "already closed", "trade_id": trade_id}
    stake = float(trade["stake_amount"] or 0)
    odds = float(trade["odds"] or 1.85)
    pnl = stake * (odds - 1.0) if outcome == 1 else -stake
    c.execute(
        """UPDATE paper_trades SET outcome=?, pnl=?, status='closed' WHERE id=?""",
        (outcome, pnl, trade_id),
    )
    c.execute(
        """INSERT INTO signal_outcomes
           (fixture_id, timestamp, signal, corner_prob, goal_prob, stake, odds, outcome, pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade["fixture_id"],
            time.time(),
            trade["signal"],
            trade["prob"] if "CORNER" in (trade["signal"] or "") else 0,
            trade["prob"] if "GOAL" in (trade["signal"] or "") else 0,
            trade["stake_pct"],
            odds,
            outcome,
            pnl,
        ),
    )
    conn.commit()
    _maybe_close(conn)
    return {"trade_id": trade_id, "outcome": outcome, "pnl": pnl, "status": "closed"}


def resolve_by_fixture(
    fixture_id: str,
    outcome: int,
    path: str = DB_PATH,
) -> Dict[str, Any]:
    """Fecha todos os paper trades abertos da fixture."""
    conn = get_pooled_conn(path)
    c = conn.cursor()
    c.execute(
        "SELECT id FROM paper_trades WHERE fixture_id=? AND status='open'",
        (fixture_id,),
    )
    ids = [r[0] for r in c.fetchall()]
    _maybe_close(conn)
    results = []
    for tid in ids:
        results.append(resolve_paper_trade(tid, outcome, path))
    return {"fixture_id": fixture_id, "closed": len(results), "results": results}


def list_open_trades(path: str = DB_PATH) -> List[Dict[str, Any]]:
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM paper_trades WHERE status='open' ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    _maybe_close(conn)
    return rows


def list_closed_trades(limit: int = 200, path: str = DB_PATH) -> List[Dict[str, Any]]:
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM paper_trades WHERE status='closed' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in c.fetchall()]
    _maybe_close(conn)
    return rows


def paper_summary(path: str = DB_PATH, with_metrics: bool = True) -> Dict[str, Any]:
    conn = get_pooled_conn(path)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'")
    n_open = c.fetchone()[0]
    c.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM paper_trades WHERE status='closed'")
    n_closed, total_pnl = c.fetchone()
    c.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status='closed' AND outcome=1"
    )
    n_wins = c.fetchone()[0]
    _maybe_close(conn)
    hit = (n_wins / n_closed) if n_closed else 0.0
    out = {
        "open": n_open,
        "closed": n_closed,
        "wins": n_wins,
        "hit_rate": round(hit, 4),
        "total_pnl": round(float(total_pnl or 0), 4),
    }
    if with_metrics and n_closed >= 3:
        try:
            from backtest_engine import load_from_sqlite, run_backtest
            rows = load_from_sqlite(path)
            metrics = run_backtest(rows)
            if isinstance(metrics, dict) and "error" not in metrics:
                out["metrics"] = {
                    "brier": metrics.get("brier"),
                    "roi": metrics.get("roi"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "profit_factor": metrics.get("profit_factor"),
                    "n_trades": metrics.get("n_trades"),
                }
                if metrics.get("roi") is not None and metrics["roi"] < 0:
                    out["alert"] = "ROI negativo — revisar thresholds ou parar paper"
        except Exception:
            pass
    return out


def save_calibration(
    min_prob_corner: float,
    min_prob_goal: float,
    max_stake_pct: float,
    sample_n: int,
    brier: float,
    roi: float,
    notes: str = "",
    path: str = DB_PATH,
) -> None:
    conn = get_pooled_conn(path)
    c = conn.cursor()
    c.execute(
        """INSERT INTO risk_calibration
           (ts, min_prob_corner, min_prob_goal, max_stake_pct, sample_n, brier, roi, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), min_prob_corner, min_prob_goal, max_stake_pct, sample_n, brier, roi, notes),
    )
    conn.commit()
    _maybe_close(conn)


def latest_calibration(path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM risk_calibration ORDER BY ts DESC LIMIT 1")
    row = c.fetchone()
    _maybe_close(conn)
    return dict(row) if row else None


try:
    init_schema()
except Exception as _init_exc:
    print(f"[DB] init_schema deferred: {_init_exc}")


def enqueue_feature_quality(
    fixture_id: str,
    quality: Dict[str, Any],
    captured_at: Optional[float] = None,
    path: Optional[str] = None,
) -> int:
    """Enfileira resumo de qualidade; retorna 0 quando aceito na fila."""
    path = path or DB_PATH
    now = time.time()
    sql = """INSERT INTO feature_quality_summary
       (fixture_id, captured_at, received_at, status_counts_json,
        missing_fields_json, invalid_fields_json, critical_missing_fields_json,
        legacy_default_count, schema_version, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    params = (
        str(fixture_id or ""), captured_at, now,
        json.dumps(quality.get("status_counts") or {}),
        json.dumps(quality.get("missing_fields") or []),
        json.dumps(quality.get("invalid_fields") or []),
        json.dumps(quality.get("critical_missing_fields") or []),
        int(quality.get("legacy_default_count") or 0),
        str(quality.get("schema_version") or "p0_quality_v1"), now,
    )
    return 0 if get_batch_writer(path).enqueue(sql, params) else -1


def persist_feature_quality(
    fixture_id: str,
    quality: Dict[str, Any],
    captured_at: Optional[float] = None,
    path: Optional[str] = None,
) -> int:
    """Append-only feature quality summary for one ingest (not recompute)."""
    path = path or DB_PATH
    conn = get_pooled_conn(path)
    c = conn.cursor()
    now = time.time()
    c.execute(
        """INSERT INTO feature_quality_summary
           (fixture_id, captured_at, received_at, status_counts_json,
            missing_fields_json, invalid_fields_json, critical_missing_fields_json,
            legacy_default_count, schema_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(fixture_id or ""),
            captured_at,
            now,
            json.dumps(quality.get("status_counts") or {}),
            json.dumps(quality.get("missing_fields") or []),
            json.dumps(quality.get("invalid_fields") or []),
            json.dumps(quality.get("critical_missing_fields") or []),
            int(quality.get("legacy_default_count") or 0),
            str(quality.get("schema_version") or "p0_quality_v1"),
            now,
        ),
    )
    rid = c.lastrowid
    conn.commit()
    _maybe_close(conn)
    return int(rid)


# ---------------------------------------------------------------------------
# P0 Fase 2 — Ledger append-only (raw_events, decisions, flags, model_versions)
# ---------------------------------------------------------------------------

import hashlib
import uuid


def _now() -> float:
    return time.time()


def _hash_payload(payload: Dict[str, Any]) -> str:
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(payload)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def ensure_model_version(
    model_version: str = "localai-12.7.16-p0",
    model_name: str = "LocalAIEngine",
    schema_version: str = "p0_ledger_v1",
    checksum: str = "",
    metrics: Optional[Dict[str, Any]] = None,
    notes: str = "",
    path: Optional[str] = None,
) -> str:
    """Idempotent register of model version metadata."""
    path = path or DB_PATH
    conn = get_pooled_conn(path)
    c = conn.cursor()
    c.execute("SELECT model_version FROM model_versions WHERE model_version=?", (model_version,))
    row = c.fetchone()
    if row:
        _maybe_close(conn)
        return model_version
    c.execute(
        """INSERT INTO model_versions
           (model_version, model_name, schema_version, checksum, metrics_json, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            model_version,
            model_name,
            schema_version,
            checksum or "",
            json.dumps(metrics or {}),
            notes,
            _now(),
        ),
    )
    conn.commit()
    _maybe_close(conn)
    return model_version


def append_raw_event(
    payload: Dict[str, Any],
    *,
    fixture_id: str = "",
    event_id: Optional[str] = None,
    source: str = "telemetry",
    source_ts: Optional[float] = None,
    sequence_no: Optional[int] = None,
    schema_version: str = "p0_ledger_v1",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Append-only raw event. Idempotent on event_id.
    Returns {ok, event_id, inserted, error?}.
    """
    path = path or DB_PATH
    try:
        init_schema(path)
    except Exception as exc:
        return {"ok": False, "error": f"LEDGER_UNAVAILABLE:schema:{exc}", "event_id": event_id}

    fid = str(fixture_id or payload.get("fixtureId") or payload.get("fixture") or "").strip()
    if not fid:
        return {"ok": False, "error": "fixture_id_missing", "event_id": event_id}

    if not event_id:
        event_id = str(
            payload.get("eventId")
            or payload.get("event_id")
            or payload.get("telemetryId")
            or ""
        ).strip()
    if not event_id:
        # Deterministic-ish id from hash + fixture + coarse time to reduce accidental dupes
        h = _hash_payload(payload)[:16]
        event_id = f"ev_{fid}_{h}"

    if source_ts is None:
        stamp = payload.get("capturedAt") or payload.get("captured_at") or payload.get("timestamp") or payload.get("ts")
        if isinstance(stamp, (int, float)):
            source_ts = float(stamp if stamp < 10_000_000_000 else stamp / 1000.0)
        else:
            source_ts = None

    raw_hash = _hash_payload(payload)
    received = _now()
    try:
        conn = get_conn(path)
        c = conn.cursor()
        try:
            c.execute(
                """INSERT OR IGNORE INTO raw_events
                   (event_id, fixture_id, source, source_ts, received_ts, sequence_no,
                    schema_version, raw_hash, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    fid,
                    source,
                    source_ts,
                    received,
                    sequence_no,
                    schema_version,
                    raw_hash,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    received,
                ),
            )
            conn.commit()
            inserted = True
        except sqlite3.IntegrityError:
            conn.rollback()
            inserted = False
        _maybe_close(conn)
        return {"ok": True, "event_id": event_id, "inserted": inserted, "raw_hash": raw_hash, "fixture_id": fid}
    except Exception as exc:
        # V23: caixa-forte outbox se o SQLite travar
        try:
            from outbox_telemetry import save_to_outbox
            save_to_outbox({
                "event_id": event_id,
                "fixture_id": fid if "fid" in dir() else "",
                "source": "outbox_fallback",
                "received_ts": _now() if "_now" in dir() else __import__("time").time(),
                "payload": payload if "payload" in dir() else {},
            })
        except Exception:
            pass
        return {"ok": False, "error": f"LEDGER_UNAVAILABLE:{exc}", "event_id": event_id}


def append_decision_log(
    analysis: Dict[str, Any],
    *,
    event_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    model_version: str = "localai-12.7.16-p0",
    policy: str = "p0_hard_gates",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persist decision before chat/voice/Telegram. Idempotent on decision_id.
    If ledger unavailable → ok=False, error=LEDGER_UNAVAILABLE (caller should BLOCK).
    """
    path = path or DB_PATH
    try:
        init_schema(path)
        ensure_model_version(model_version=model_version, path=path)
    except Exception as exc:
        return {"ok": False, "error": f"LEDGER_UNAVAILABLE:schema:{exc}", "decision_id": decision_id}

    fid = str(analysis.get("fixtureId") or analysis.get("fixture_id") or "").strip()
    if not fid:
        return {"ok": False, "error": "fixture_id_missing", "decision_id": decision_id}

    signal = str(analysis.get("signal") or analysis.get("decision") or "HOLD")
    integrity = analysis.get("data_integrity") or {}
    flags = list(integrity.get("issues") or []) + list(integrity.get("warnings") or [])
    if analysis.get("skill_kills"):
        flags = flags + list(analysis.get("skill_kills") or [])

    if not decision_id:
        decision_id = str(analysis.get("decision_id") or "").strip()
    if not decision_id:
        decision_id = f"dec_{fid}_{uuid.uuid4().hex[:12]}"

    ts = _now()
    try:
        conn = get_conn(path)
        c = conn.cursor()
        try:
            c.execute(
                """INSERT INTO decision_logs
                   (decision_id, event_id, fixture_id, ts, signal, decision,
                    corner_prob, goal_prob, kelly, approved, integrity_status,
                    integrity_json, flags_json, feature_quality_json, model_version,
                    policy, reason, analysis_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    event_id,
                    fid,
                    ts,
                    signal,
                    str(analysis.get("decision") or signal),
                    float(analysis.get("corner_prob") or 0.0),
                    float(analysis.get("goal_prob") or 0.0),
                    float(analysis.get("kelly") or 0.0),
                    1 if analysis.get("approved") else 0,
                    str(integrity.get("status") or ""),
                    json.dumps(integrity, ensure_ascii=False, default=str),
                    json.dumps(flags, ensure_ascii=False, default=str),
                    json.dumps(analysis.get("feature_quality") or {}, ensure_ascii=False, default=str),
                    model_version,
                    policy,
                    str(analysis.get("reason") or analysis.get("explanation") or ""),
                    json.dumps(analysis, ensure_ascii=False, default=str),
                    ts,
                ),
            )
            conn.commit()
            inserted = True
        except sqlite3.IntegrityError:
            conn.rollback()
            inserted = False
        _maybe_close(conn)
        return {
            "ok": True,
            "decision_id": decision_id,
            "event_id": event_id,
            "inserted": inserted,
            "fixture_id": fid,
        }
    except Exception as exc:
        return {"ok": False, "error": f"LEDGER_UNAVAILABLE:{exc}", "decision_id": decision_id}


def append_data_quality_flag(
    flag_code: str,
    *,
    severity: str = "WARN",
    fixture_id: str = "",
    decision_id: str = "",
    event_id: str = "",
    message: str = "",
    details: Optional[Dict[str, Any]] = None,
    flag_id: Optional[str] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    path = path or DB_PATH
    try:
        init_schema(path)
    except Exception as exc:
        return {"ok": False, "error": f"LEDGER_UNAVAILABLE:{exc}"}
    flag_id = flag_id or f"flag_{uuid.uuid4().hex[:12]}"
    ts = _now()
    try:
        conn = get_conn(path)
        c = conn.cursor()
        try:
            c.execute(
                """INSERT INTO data_quality_flags
                   (flag_id, fixture_id, decision_id, event_id, flag_code, severity,
                    message, details_json, ts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    flag_id,
                    fixture_id,
                    decision_id,
                    event_id,
                    flag_code,
                    severity,
                    message,
                    json.dumps(details or {}, ensure_ascii=False, default=str),
                    ts,
                    ts,
                ),
            )
            conn.commit()
            inserted = True
        except sqlite3.IntegrityError:
            conn.rollback()
            inserted = False
        _maybe_close(conn)
        return {"ok": True, "flag_id": flag_id, "inserted": inserted}
    except Exception as exc:
        return {"ok": False, "error": f"LEDGER_UNAVAILABLE:{exc}"}


def list_raw_events(fixture_id: str, path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or DB_PATH
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM raw_events WHERE fixture_id=? ORDER BY received_ts ASC, id ASC",
        (str(fixture_id),),
    )
    rows = [dict(r) for r in c.fetchall()]
    _maybe_close(conn)
    return rows


def list_decision_logs(fixture_id: str, path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or DB_PATH
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM decision_logs WHERE fixture_id=? ORDER BY ts ASC, id ASC",
        (str(fixture_id),),
    )
    rows = [dict(r) for r in c.fetchall()]
    _maybe_close(conn)
    return rows


def get_decision_log(decision_id: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = path or DB_PATH
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM decision_logs WHERE decision_id=?", (decision_id,))
    row = c.fetchone()
    _maybe_close(conn)
    return dict(row) if row else None



def upsert_label(
    label_row: Dict[str, Any],
    *,
    fixture_id: str,
    source_event_id: str = "",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotent label write. UNIQUE(label_id).
    label may be 0, 1, or None (censored → stored as SQL NULL).
    Never coerces censored/NULL into 0.
    """
    path = path or DB_PATH
    try:
        from target_labels import label_row_id, LABEL_VERSION, assert_not_over_under_product
    except Exception:
        from target_labels import label_row_id, LABEL_VERSION, assert_not_over_under_product  # type: ignore

    product = str(label_row.get("product") or "")
    assert_not_over_under_product(product)

    horizon = int(label_row.get("horizon_sec") or 0)
    window_start = label_row.get("window_start_ts")
    version = str(label_row.get("label_version") or LABEL_VERSION)
    lid = str(label_row.get("label_id") or label_row_id(fixture_id, horizon, window_start, version))

    censored = 1 if label_row.get("censored") else 0
    raw_label = label_row.get("label")
    if censored:
        # force NULL label when censored — never store 0 as RED for incomplete windows
        store_label = None
    else:
        if raw_label is None:
            store_label = None
        else:
            store_label = int(raw_label)

    now = time.time()
    try:
        conn = get_pooled_conn(path)
        c = conn.cursor()
        # Upsert: if exists, update fields but keep created_at
        c.execute("SELECT id, created_at FROM labels WHERE label_id=?", (lid,))
        existing = c.fetchone()
        if existing:
            c.execute(
                """UPDATE labels SET
                    fixture_id=?, target_name=?, product=?, horizon_sec=?,
                    window_start_ts=?, window_end_ts=?, match_seconds_start=?,
                    label=?, censored=?, censor_reason=?, window_complete=?,
                    label_version=?, source_event_id=?, details_json=?, updated_at=?
                   WHERE label_id=?""",
                (
                    str(fixture_id),
                    str(label_row.get("target_name") or "next_corner_within_horizon"),
                    product,
                    horizon,
                    window_start,
                    label_row.get("window_end_ts"),
                    label_row.get("match_seconds_start"),
                    store_label,
                    censored,
                    label_row.get("censor_reason"),
                    1 if label_row.get("window_complete") else 0,
                    version,
                    source_event_id or "",
                    json.dumps(label_row, ensure_ascii=False, default=str),
                    now,
                    lid,
                ),
            )
            inserted = False
        else:
            c.execute(
                """INSERT INTO labels
                   (label_id, fixture_id, target_name, product, horizon_sec,
                    window_start_ts, window_end_ts, match_seconds_start,
                    label, censored, censor_reason, window_complete, label_version,
                    source_event_id, details_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lid,
                    str(fixture_id),
                    str(label_row.get("target_name") or "next_corner_within_horizon"),
                    product,
                    horizon,
                    window_start,
                    label_row.get("window_end_ts"),
                    label_row.get("match_seconds_start"),
                    store_label,
                    censored,
                    label_row.get("censor_reason"),
                    1 if label_row.get("window_complete") else 0,
                    version,
                    source_event_id or "",
                    json.dumps(label_row, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
            inserted = True
        conn.commit()
        _maybe_close(conn)
        return {
            "ok": True,
            "label_id": lid,
            "inserted": inserted,
            "label": store_label,
            "censored": censored,
            "censor_reason": label_row.get("censor_reason"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "label_id": lid}


def list_labels(
    fixture_id: str,
    *,
    horizon_sec: Optional[int] = None,
    path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    path = path or DB_PATH
    conn = get_pooled_conn(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if horizon_sec is not None:
        c.execute(
            "SELECT * FROM labels WHERE fixture_id=? AND horizon_sec=? ORDER BY window_start_ts ASC",
            (str(fixture_id), int(horizon_sec)),
        )
    else:
        c.execute(
            "SELECT * FROM labels WHERE fixture_id=? ORDER BY horizon_sec, window_start_ts ASC",
            (str(fixture_id),),
        )
    rows = [dict(r) for r in c.fetchall()]
    _maybe_close(conn)
    return rows


# ---------------------------------------------------------------------------
# V23 P0: batch writer for high-frequency event ingest (reduces lock contention)
# ---------------------------------------------------------------------------
import hashlib
from dataclasses import dataclass
from queue import Empty, Queue

_write_q: "Queue[WriteJob]" = Queue(maxsize=20000)
_stop_writer = threading.Event()
_writer_thread: threading.Thread | None = None


@dataclass(slots=True)
class WriteJob:
    sql: str
    params: tuple


def start_writer() -> None:
    """Start daemon batch writer (idempotent). Call once at engine boot."""
    global _writer_thread
    if _writer_thread and _writer_thread.is_alive():
        return
    _writer_thread = threading.Thread(
        target=_writer_loop, name="aura-sqlite-writer", daemon=True
    )
    _writer_thread.start()


def stop_writer() -> None:
    _stop_writer.set()


def _writer_loop() -> None:
    conn = get_conn()
    cur = conn.cursor()
    while not _stop_writer.is_set():
        batch: list = []
        try:
            batch.append(_write_q.get(timeout=0.10))
        except Empty:
            continue
        deadline = time.time() + 0.006
        while len(batch) < 128 and time.time() < deadline:
            try:
                batch.append(_write_q.get_nowait())
            except Empty:
                break
        cur.execute("BEGIN")
        try:
            for job in batch:
                cur.execute(job.sql, job.params)
            cur.execute("COMMIT")
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            import logging
            logging.getLogger("aura.data_store").exception("batch writer flush failed")


_dropped_count = 0  # V24: observable drop counter (GIL-safe increment)


def get_dropped_events_count() -> int:
    return int(_dropped_count)


def enqueue_raw_event(
    event_id: str,
    fixture_id: str,
    payload: dict,
    source: str = "telemetry",
) -> None:
    """Non-blocking enqueue for raw event persistence."""
    global _dropped_count
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        _write_q.put_nowait(
            WriteJob(
                """
                INSERT OR IGNORE INTO raw_events
                (event_id, fixture_id, source, source_ts, received_ts, raw_hash, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    fixture_id,
                    source,
                    payload.get("timestamp_unix"),
                    time.time(),
                    raw_hash,
                    raw,
                    time.time(),
                ),
            )
        )
    except Exception:
        _dropped_count += 1  # fail-closed + observable via get_dropped_events_count()


# ---- V23 drop-in: tabela quente de fingerprints (RAM-first para QuantBrain) ----
_HOT_FINGERPRINTS: dict = {}
_HOT_FP_MAX = 4096
_HOT_LOCK = threading.RLock()


def fp_get(hash_id: str):
    """RAM-first fingerprint lookup; disco só em miss. Negative cache amortizado via None."""
    with _HOT_LOCK:
        hit = _HOT_FINGERPRINTS.get(hash_id)
        now = time.time()
        if hit and (now - float(hit.get("ts") or 0)) > 900.0:
            _HOT_FINGERPRINTS.pop(hash_id, None)
            hit = None
        if hit is not None:
            return hit.get("value")
    try:
        row = get_thread_safe_conn().execute(
            "SELECT occurrences, corners_happened FROM pattern_fingerprints WHERE hash_id=?",
            (hash_id,),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        result = None
    else:
        occ = int(row[0] or 0)
        happened = int(row[1] or 0)
        result = {"occurrences": occ, "prob": round((happened / occ) * 100.0, 1) if occ else 0.0}
    with _HOT_LOCK:
        _HOT_FINGERPRINTS[hash_id] = {"ts": time.time(), "value": result}
        while len(_HOT_FINGERPRINTS) > _HOT_FP_MAX:
            _HOT_FINGERPRINTS.pop(next(iter(_HOT_FINGERPRINTS)))
    return result

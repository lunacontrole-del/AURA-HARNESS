# -*- coding: utf-8 -*-
"""
PILAR 1 - Persistência Híbrida (Data Store)
AURA QUANT-X v12.7.0-RECONSOLIDADO

SQLite em RAM + double-buffering + flush assíncrono. A API moderna `write()`
é usada pelo Engine integrado; a API de compatibilidade (`log_telemetry`,
`log_paper_trade` etc.) é mantida para as suítes e módulos dos anexos.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

import logging
logger = logging.getLogger("aura.pilar1.hybrid_store")

BUFFER_SIZE_LIMIT = 10000
FLUSH_INTERVAL_SECONDS = 60.0
DISK_PATH_DEFAULT = os.environ.get("AURA_DB_PATH", os.path.join(os.path.dirname(__file__), "artifacts", "aura_telemetry.db"))


@dataclass
class TelemetryRecord:
    timestamp: float
    match_id: str
    odds: float
    odds_velocity: float
    asian_line: float
    corner_count: int
    market_type: str
    extra: Dict[str, Any] = field(default_factory=dict)


class RingBuffer:
    """Ring buffer de dicionários usado pela API dos anexos."""

    def __init__(self, capacity: int):
        if int(capacity) <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = int(capacity)
        self._buffer: deque = deque(maxlen=self.capacity)
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def push(self, item: Dict[str, Any]) -> bool:
        with self._lock:
            overwritten = len(self._buffer) >= self.capacity
            self._buffer.append(item)
            return overwritten

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> int:
        with self._lock:
            n = len(self._buffer)
            self._buffer.clear()
            return n

    def find_by_correlation(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for item in reversed(self._buffer):
                if item.get("correlation_id") == correlation_id:
                    return item
        return None


class DoubleBufferRing:
    """Ring buffer de `TelemetryRecord` com troca atômica para flush."""

    def __init__(self, capacity: int = BUFFER_SIZE_LIMIT):
        self.capacity = max(1, int(capacity))
        self._active: deque = deque(maxlen=self.capacity)
        self._passive: deque = deque(maxlen=self.capacity)
        self._lock = threading.Lock()
        self._write_count = np.int64(0)
        self._flush_count = np.int64(0)

    def append(self, record: TelemetryRecord) -> bool:
        with self._lock:
            self._active.append(record)
            self._write_count += 1
            return len(self._active) >= self.capacity

    def swap_and_drain(self) -> List[TelemetryRecord]:
        with self._lock:
            self._active, self._passive = self._passive, self._active
            drained = list(self._passive)
            self._passive.clear()
            self._flush_count += 1
            return drained

    def size(self) -> int:
        with self._lock:
            return len(self._active)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"active_size": len(self._active), "writes": int(self._write_count), "flushes": int(self._flush_count)}


class HybridDataStore:
    """Store híbrido em RAM com persistência final/por callback."""

    def __init__(
        self,
        disk_path: str = DISK_PATH_DEFAULT,
        buffer_limit: int = BUFFER_SIZE_LIMIT,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        persist_callback: Optional[Callable[[List[TelemetryRecord]], None]] = None,
        buffer_size: Optional[int] = None,
    ):
        self.disk_path = str(disk_path)
        self.buffer_limit = max(1, int(buffer_size if buffer_size is not None else buffer_limit))
        self._buffer_size = self.buffer_limit
        self.flush_interval = float(flush_interval)
        self._persist_callback = persist_callback
        self._ring = DoubleBufferRing(capacity=self.buffer_limit)
        self._mem_conn: Optional[sqlite3.Connection] = None
        self._ram_conn: Optional[sqlite3.Connection] = None
        self._disk_conn: Optional[sqlite3.Connection] = None
        self._flush_lock = threading.RLock()
        self._legacy_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._daemon: Optional[threading.Thread] = None
        self._last_flush_ts = time.time()
        self._initialized = False
        self._closed = False
        self._legacy_pending = 0
        self._total_inserted = 0
        self._total_flushed = 0
        self._flush_count = 0
        self._error_count = 0
        self._init_memory_db()
        # A conexão fria é aberta uma vez na inicialização; o caminho quente
        # continua escrevendo somente na RAM e no ring buffer.
        self._get_disk_connection()
        self._start_daemon()

    def _init_memory_db(self) -> None:
        self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._ram_conn = self._mem_conn
        self._mem_conn.execute("PRAGMA journal_mode=MEMORY")
        self._mem_conn.execute("PRAGMA synchronous=OFF")
        self._mem_conn.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                match_id TEXT NOT NULL,
                odds REAL,
                odds_velocity REAL,
                asian_line REAL,
                corner_count INTEGER,
                market_type TEXT,
                extra TEXT
            );
            CREATE TABLE IF NOT EXISTS logs_telemetria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id TEXT,
                timestamp REAL,
                match_minute REAL,
                payload_json TEXT NOT NULL,
                correlation_id TEXT,
                signal TEXT,
                corner_prob REAL,
                goal_prob REAL,
                stake REAL,
                asian_corner_line REAL,
                asian_corner_odds REAL,
                odds_velocity REAL
            );
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                fixture_id TEXT,
                timestamp REAL,
                signal TEXT,
                outcome INTEGER,
                pnl REAL,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id TEXT,
                trade_id TEXT UNIQUE,
                created_at REAL,
                signal_decision TEXT,
                stake REAL,
                kelly_fraction REAL,
                outcome TEXT,
                profit_loss REAL,
                status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                message TEXT,
                event_level TEXT,
                correlation_id TEXT,
                ts REAL
            );
        """)
        self._mem_conn.commit()
        self._initialized = True
        logger.info("SQLite :memory: inicializado (zero I/O em disco)")

    def _start_daemon(self) -> None:
        self._daemon = threading.Thread(target=self._flush_loop, name="AuraHybridFlushDaemon", daemon=True)
        self._daemon.start()
        logger.info("Thread daemon de flush iniciada")

    def start(self) -> "HybridDataStore":
        if self._closed:
            raise RuntimeError("HybridDataStore já encerrado")
        if self._daemon is None or not self._daemon.is_alive():
            self._stop_event.clear()
            self._start_daemon()
        return self

    def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._ring.size() >= self.buffer_limit or time.time() - self._last_flush_ts >= self.flush_interval:
                    self._do_flush()
                if self._legacy_pending >= self.buffer_limit:
                    self._mark_legacy_flush()
                time.sleep(0.25)
            except Exception as exc:
                self._error_count += 1
                logger.error("Erro no loop de flush: %s", exc)

    def _mark_legacy_flush(self) -> int:
        with self._legacy_lock:
            count = self._legacy_pending
            if not count:
                return 0
            self._legacy_pending = 0
            self._total_flushed += count
            self._flush_count += 1
            self._last_flush_ts = time.time()
            return count

    def _do_flush(self) -> int:
        with self._flush_lock:
            records = self._ring.swap_and_drain()
            if not records:
                return 0
            rows = [(r.timestamp, r.match_id, r.odds, r.odds_velocity, r.asian_line, r.corner_count, r.market_type, json.dumps(r.extra, ensure_ascii=False, default=str)) for r in records]
            assert self._mem_conn is not None
            cur = self._mem_conn.cursor()
            try:
                cur.execute("BEGIN TRANSACTION")
                cur.executemany("INSERT INTO telemetry (ts, match_id, odds, odds_velocity, asian_line, corner_count, market_type, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                cur.execute("COMMIT")
                if self._persist_callback is not None:
                    self._persist_callback(records)
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
                self._error_count += 1
                raise
            self._total_flushed += len(records)
            self._flush_count += 1
            self._last_flush_ts = time.time()
            logger.info("Flush concluído: %d registros (memória + persistência=%s)", len(records), bool(self._persist_callback))
            return len(records)

    def write(self, match_id: str, odds: float, odds_velocity: float = 0.0, asian_line: float = 0.0, corner_count: int = 0, market_type: str = "asian_corner", extra: Optional[Dict[str, Any]] = None) -> None:
        rec = TelemetryRecord(time.time(), str(match_id), float(odds), float(odds_velocity), float(asian_line), int(corner_count), str(market_type), extra or {})
        with self._legacy_lock:
            self._total_inserted += 1
        if self._ring.append(rec):
            threading.Thread(target=self._do_flush, name="AuraHybridFlush", daemon=True).start()

    def log_telemetry(self, payload: Dict[str, Any]) -> str:
        data = dict(payload or {})
        correlation = str(data.get("correlation_id") or f"tel_{uuid.uuid4().hex[:16]}")
        fixture = str(data.get("fixture_id") or data.get("fixtureId") or data.get("match_id") or "")
        timestamp = float(data.get("timestamp") or time.time())
        match_minute = data.get("match_minute", data.get("minute"))
        with self._legacy_lock:
            assert self._mem_conn is not None
            self._mem_conn.execute(
                """INSERT INTO logs_telemetria
                   (fixture_id, timestamp, match_minute, payload_json, correlation_id,
                    signal, corner_prob, goal_prob, stake, asian_corner_line,
                    asian_corner_odds, odds_velocity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fixture, timestamp, match_minute, json.dumps(data, ensure_ascii=False, default=str), correlation,
                 data.get("signal") or data.get("decision"), data.get("corner_prob"), data.get("goal_prob"),
                 data.get("stake"), data.get("asian_corner_line"), data.get("asian_corner_odds"), data.get("odds_velocity")),
            )
            self._mem_conn.commit()
            self._legacy_pending += 1
            self._total_inserted += 1
            if self._legacy_pending >= self.buffer_limit:
                self._mark_legacy_flush()
        return correlation

    def get_telemetry_by_fixture(self, fixture_id: str) -> List[Dict[str, Any]]:
        with self._legacy_lock:
            assert self._mem_conn is not None
            rows = self._mem_conn.execute("SELECT timestamp, payload_json, correlation_id FROM logs_telemetria WHERE fixture_id=? ORDER BY timestamp DESC, id DESC", (str(fixture_id),)).fetchall()
        result = []
        for timestamp, raw, correlation in rows:
            try:
                item = json.loads(raw)
            except Exception:
                item = {}
            item["timestamp"] = timestamp
            item["correlation_id"] = correlation
            result.append(item)
        return result

    def log_paper_trade(self, fixture_id: str, trade_id: str, signal_decision: str, stake: float, kelly_fraction: float = 0.0, **_: Any) -> str:
        with self._legacy_lock:
            assert self._mem_conn is not None
            self._mem_conn.execute("INSERT OR REPLACE INTO paper_trades (fixture_id, trade_id, created_at, signal_decision, stake, kelly_fraction, status) VALUES (?, ?, ?, ?, ?, ?, 'OPEN')", (str(fixture_id), str(trade_id), time.time(), str(signal_decision), float(stake), float(kelly_fraction)))
            self._mem_conn.commit()
            self._legacy_pending += 1
            self._total_inserted += 1
        return str(trade_id)

    def get_open_trades(self, fixture_id: str) -> List[Dict[str, Any]]:
        with self._legacy_lock:
            assert self._mem_conn is not None
            cur = self._mem_conn.execute("SELECT fixture_id, trade_id, created_at, signal_decision, stake, kelly_fraction, outcome, profit_loss, status FROM paper_trades WHERE fixture_id=? AND status='OPEN' ORDER BY created_at DESC", (str(fixture_id),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close_paper_trade(self, trade_id: str, outcome: str, profit_loss: float) -> bool:
        with self._legacy_lock:
            assert self._mem_conn is not None
            cur = self._mem_conn.execute("UPDATE paper_trades SET outcome=?, profit_loss=?, status='CLOSED' WHERE trade_id=? AND status='OPEN'", (str(outcome), float(profit_loss), str(trade_id)))
            self._mem_conn.commit()
            return cur.rowcount > 0

    def log_system_event(self, event_type: str, message: str, event_level: str = "INFO", correlation_id: Optional[str] = None) -> str:
        correlation = correlation_id or f"evt_{uuid.uuid4().hex[:16]}"
        with self._legacy_lock:
            assert self._mem_conn is not None
            self._mem_conn.execute("INSERT INTO system_events (event_type, message, event_level, correlation_id, ts) VALUES (?, ?, ?, ?, ?)", (str(event_type), str(message), str(event_level), correlation, time.time()))
            self._mem_conn.commit()
            self._legacy_pending += 1
            self._total_inserted += 1
        return correlation

    def force_flush(self) -> int:
        total = self._do_flush()
        with self._legacy_lock:
            legacy_pending = self._legacy_pending
        if legacy_pending and self._disk_conn is None:
            self._error_count += 1
            logger.error("Conexão fria indisponível durante flush do Pilar 1")
            return total
        if self._persist_callback is None and self._disk_conn is not None and self._mem_conn is not None:
            try:
                self._mem_conn.backup(self._disk_conn)
                self._disk_conn.commit()
            except Exception as exc:
                self._error_count += 1
                logger.error("Falha ao sincronizar store standalone no disco: %s", exc)
        total += self._mark_legacy_flush()
        return total

    def force_flush_sync(self) -> int:
        return self.force_flush()

    def atomic_persist_to_disk(self) -> str:
        self.force_flush()
        if self._persist_callback is not None:
            return self.disk_path
        os.makedirs(os.path.dirname(self.disk_path) or ".", exist_ok=True)
        tmp_path = self.disk_path + ".tmp"
        assert self._mem_conn is not None
        disk = sqlite3.connect(tmp_path)
        try:
            self._mem_conn.backup(disk)
            disk.commit()
        finally:
            disk.close()
        os.replace(tmp_path, self.disk_path)
        logger.info("Persistência atômica concluída: %s", self.disk_path)
        return self.disk_path

    def stop(self, wait_flush: bool = True) -> None:
        self.shutdown(wait_flush=wait_flush)

    def shutdown(self, wait_flush: bool = True) -> None:
        if self._closed:
            return
        self._stop_event.set()
        if self._daemon and self._daemon.is_alive():
            self._daemon.join(timeout=5.0)
        if wait_flush:
            self.atomic_persist_to_disk()
        self._closed = True
        if self._mem_conn:
            self._mem_conn.close()
        logger.info("HybridDataStore encerrado com segurança")

    def _get_disk_connection(self) -> sqlite3.Connection:
        if self._disk_conn is None:
            os.makedirs(os.path.dirname(self.disk_path) or ".", exist_ok=True)
            self._disk_conn = sqlite3.connect(self.disk_path, check_same_thread=False)
            self._disk_conn.row_factory = sqlite3.Row
            self._disk_conn.execute("CREATE TABLE IF NOT EXISTS logs_telemetria (id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id TEXT, timestamp REAL, match_minute REAL, payload_json TEXT NOT NULL, correlation_id TEXT, signal TEXT, corner_prob REAL, goal_prob REAL, stake REAL, asian_corner_line REAL, asian_corner_odds REAL, odds_velocity REAL)")
            self._disk_conn.commit()
        return self._disk_conn

    def get_stats(self) -> Dict[str, Any]:
        return self.stats()

    def stats(self) -> Dict[str, Any]:
        with self._legacy_lock:
            pending = self._legacy_pending
            total_inserted = self._total_inserted
        ring = self._ring.stats()
        return {
            "ring": ring,
            "buffer_count": ring["active_size"] + pending,
            "buffer_capacity": self.buffer_limit,
            "buffer_utilization_pct": min(100.0, ((ring["active_size"] + pending) / self.buffer_limit) * 100.0),
            "total_inserted": total_inserted,
            "total_flushed": self._total_flushed,
            "flush_count": self._flush_count,
            "error_count": self._error_count,
            "time_since_flush_ms": (time.time() - self._last_flush_ts) * 1000.0,
            "disk_db_path": self.disk_path,
            "disk_connected": self._disk_conn is not None or os.path.exists(self.disk_path),
            "schema_version": "pilar1_v12.7.0",
            "last_flush_ago_s": time.time() - self._last_flush_ts,
            "initialized": self._initialized,
        }


_store_instance: Optional[HybridDataStore] = None
_store_lock = threading.Lock()


def get_hybrid_store() -> HybridDataStore:
    global _store_instance
    with _store_lock:
        if _store_instance is None or _store_instance._closed:
            _store_instance = HybridDataStore()
        return _store_instance


def shutdown_hybrid_store() -> None:
    global _store_instance
    with _store_lock:
        if _store_instance is not None:
            _store_instance.shutdown()
            _store_instance = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    store = get_hybrid_store()
    store.write("MATCH_1", 1.90, odds_velocity=0.2, asian_line=8.5, corner_count=4)
    print(store.stats())
    store.shutdown()

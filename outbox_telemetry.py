# engine/outbox_telemetry.py — V23 caixa-forte de telemetria (anti-perda em pico de DB)
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("aura.outbox")

_ROOT = Path(__file__).resolve().parent
OUTBOX_FILE = str(_ROOT / "data" / "telemetry_outbox.jsonl")
Path(OUTBOX_FILE).parent.mkdir(parents=True, exist_ok=True)


def save_to_outbox(event_data: Dict[str, Any]) -> None:
    """Salva o evento no disco quando o DB esta travado."""
    try:
        Path(OUTBOX_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event_data, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.critical("IMPOSSIVEL SALVAR NO OUTBOX. DADOS PERDIDOS: %s", e)


def flush_outbox_to_db(db_path: str) -> int:
    """Sincrono: esvazia outbox no SQLite com INSERT OR IGNORE. Retorna linhas processadas."""
    if not os.path.exists(OUTBOX_FILE):
        return 0
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            processed = 0
            with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    conn.execute(
                        """INSERT OR IGNORE INTO raw_events
                           (event_id, fixture_id, source, source_ts, received_ts, sequence_no,
                            schema_version, raw_hash, payload_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event.get("event_id"),
                            event.get("fixture_id") or "",
                            event.get("source") or "outbox",
                            event.get("source_ts"),
                            event.get("received_ts") or time.time(),
                            event.get("sequence_no"),
                            event.get("schema_version"),
                            event.get("raw_hash"),
                            event.get("payload") if isinstance(event.get("payload"), str) else json.dumps(event.get("payload") or {}, ensure_ascii=False, default=str),
                            event.get("received_ts") or time.time(),
                        ),
                    )
                    processed += 1
            conn.commit()
        finally:
            conn.close()
        try:
            os.remove(OUTBOX_FILE)
        except OSError:
            pass
        logger.info("Outbox de telemetria descarregado: %s eventos", processed)
        return processed
    except Exception as e:
        logger.error("Falha ao descarregar outbox (tentara na proxima): %s", e)
        return 0

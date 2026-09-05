"""P2 Fase 6 — Memória autorizada com consentimento, origem, confiança, TTL e exclusão."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from data_store import get_conn, init_schema, DB_PATH


def _ensure_table(path: Optional[str] = None) -> None:
    path = path or DB_PATH
    init_schema(path)
    conn = get_conn(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL UNIQUE,
            fixture_id TEXT,
            content TEXT NOT NULL,
            origin TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            consent INTEGER NOT NULL DEFAULT 0,
            ttl_sec INTEGER NOT NULL DEFAULT 86400,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.commit()
    conn.close()


def remember(
    content: str,
    *,
    origin: str,
    consent: bool,
    confidence: float = 0.5,
    ttl_sec: int = 86400,
    fixture_id: str = "",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    if not consent:
        return {"ok": False, "error": "CONSENT_REQUIRED"}
    if not content or not origin:
        return {"ok": False, "error": "content_and_origin_required"}
    _ensure_table(path)
    mid = f"mem_{uuid.uuid4().hex[:12]}"
    now = time.time()
    exp = now + max(60, int(ttl_sec))
    conn = get_conn(path or DB_PATH)
    conn.execute(
        """INSERT INTO memory_records
           (memory_id, fixture_id, content, origin, confidence, consent, ttl_sec, created_at, expires_at, deleted)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        (mid, fixture_id, str(content)[:2000], str(origin)[:200], float(confidence), 1, int(ttl_sec), now, exp),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "memory_id": mid, "expires_at": exp}


def recall(
    *,
    fixture_id: str = "",
    limit: int = 20,
    path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    _ensure_table(path)
    now = time.time()
    conn = get_conn(path or DB_PATH)
    conn.row_factory = None
    if fixture_id:
        rows = conn.execute(
            """SELECT memory_id, fixture_id, content, origin, confidence, expires_at
               FROM memory_records
               WHERE deleted=0 AND expires_at>? AND (fixture_id=? OR fixture_id='' OR fixture_id IS NULL)
               ORDER BY created_at DESC LIMIT ?""",
            (now, fixture_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT memory_id, fixture_id, content, origin, confidence, expires_at
               FROM memory_records
               WHERE deleted=0 AND expires_at>?
               ORDER BY created_at DESC LIMIT ?""",
            (now, int(limit)),
        ).fetchall()
    conn.close()
    return [
        {
            "memory_id": r[0],
            "fixture_id": r[1],
            "content": r[2],
            "origin": r[3],
            "confidence": r[4],
            "expires_at": r[5],
        }
        for r in rows
    ]


def forget(memory_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    _ensure_table(path)
    conn = get_conn(path or DB_PATH)
    cur = conn.execute(
        "UPDATE memory_records SET deleted=1 WHERE memory_id=?",
        (memory_id,),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "deleted": n}

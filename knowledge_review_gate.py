"""AURA knowledge review gate.

PAPER TRADE / PLAN_ONLY / execution_allowed=false

Nenhuma aprovação é automática. Claims só entram no agente depois de
revisão humana explícita. O módulo existe para o engine importar
`router` em server.py e para scripts/aura_knowledge_review.py.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

PAPER_TRADE = True
PLAN_ONLY = True
EXECUTION_ALLOWED = False


def _root() -> Path:
    env = os.environ.get("AURA_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


def _inbox() -> Path:
    path = _root() / "knowledge" / "inbox" / "knowledge_candidates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def _approved() -> Path:
    path = _root() / "knowledge" / "approved" / "knowledge.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def _decisions() -> Path:
    path = _root() / "knowledge" / "review_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.exists():
        return items
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _candidate_id(item: dict[str, Any], index: int) -> str:
    for key in ("id", "candidate_id", "claim_id"):
        value = item.get(key)
        if value:
            return str(value)
    return f"cand-{index+1:04d}"


def status() -> dict[str, Any]:
    pending_items = pending(10_000)
    approved_items = _read_jsonl(_approved())
    return {
        "ok": True,
        "pending_human_review": len(pending_items),
        "approved_for_agent": len(approved_items),
        "rejected": sum(1 for item in _read_jsonl(_decisions()) if item.get("decision") == "reject"),
        "execution_allowed": False,
        "paper_trade": True,
        "plan_only": True,
        "auto_approve": False,
        "inbox": str(_inbox()),
        "approved": str(_approved()),
    }


def pending(limit: int = 20) -> list[dict[str, Any]]:
    decided = {
        str(item.get("candidate_id"))
        for item in _read_jsonl(_decisions())
        if item.get("candidate_id")
    }
    out: list[dict[str, Any]] = []
    for index, item in enumerate(_read_jsonl(_inbox())):
        cid = _candidate_id(item, index)
        if cid in decided:
            continue
        row = dict(item)
        row["candidate_id"] = cid
        out.append(row)
        if len(out) >= max(1, int(limit)):
            break
    return out


def context(query: str = "", limit: int = 8) -> dict[str, Any]:
    approved_items = _read_jsonl(_approved())
    needle = (query or "").strip().lower()
    hits = []
    for item in approved_items:
        blob = json.dumps(item, ensure_ascii=False).lower()
        if not needle or needle in blob:
            hits.append(item)
        if len(hits) >= max(1, int(limit)):
            break
    return {
        "query": query,
        "items": hits,
        "count": len(hits),
        "execution_allowed": False,
        "paper_trade": True,
    }


def decide(
    candidate_id: str,
    reviewer: str,
    decision: str,
    note: str,
    validation_reference: str = "",
) -> dict[str, Any]:
    decision_norm = str(decision or "").strip().lower()
    if decision_norm not in {"approve", "reject"}:
        raise ValueError("decision deve ser approve ou reject")
    if not reviewer or not note:
        raise ValueError("reviewer e note são obrigatórios")

    found = None
    for index, item in enumerate(_read_jsonl(_inbox())):
        if _candidate_id(item, index) == str(candidate_id):
            found = dict(item)
            found["candidate_id"] = str(candidate_id)
            break
    if found is None:
        raise KeyError(f"candidato não encontrado: {candidate_id}")

    record = {
        "candidate_id": str(candidate_id),
        "reviewer": reviewer,
        "decision": decision_norm,
        "note": note,
        "validation_reference": validation_reference,
        "at": datetime.now(timezone.utc).isoformat(),
        "execution_allowed": False,
        "paper_trade": True,
        "plan_only": True,
    }
    _append_jsonl(_decisions(), record)
    if decision_norm == "approve":
        approved = dict(found)
        approved.update(
            {
                "approved_by": reviewer,
                "approved_at": record["at"],
                "validation_reference": validation_reference,
                "execution_allowed": False,
                "paper_trade": True,
            }
        )
        _append_jsonl(_approved(), approved)
    return {"ok": True, "record": record, "status": status()}


class DecideBody(BaseModel):
    candidate_id: str
    reviewer: str
    decision: str = Field(description="approve ou reject")
    note: str
    validation_reference: str = ""


router = APIRouter(prefix="/api/knowledge", tags=["knowledge-review-gate"])


@router.get("/status")
def api_status() -> dict[str, Any]:
    return status()


@router.get("/pending")
def api_pending(limit: int = 20) -> dict[str, Any]:
    return {"items": pending(limit), "status": status()}


@router.get("/context")
def api_context(query: str = "", limit: int = 8) -> dict[str, Any]:
    return context(query, limit)


@router.post("/decide")
def api_decide(body: DecideBody) -> dict[str, Any]:
    try:
        return decide(
            body.candidate_id,
            body.reviewer,
            body.decision,
            body.note,
            body.validation_reference,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

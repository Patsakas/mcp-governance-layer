"""Audit / dashboard endpoint: GET /api/v1/requests.

Also provides POST /api/v1/requests for submitting new approval requests,
and PATCH /api/v1/requests/{id}/resolve for approving/rejecting from the dashboard.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events as event_store
from app.config import settings
from app.database import get_db
from app.decision_engine import evaluate, _normalize_intent
from app.models import HitlRequest
from app.auditor import calculate_blast_radius
from app.slack import send_slack_message
from app.telegram import send_telegram_message

router = APIRouter(prefix="/api/v1", tags=["requests"])

# Statuses that indicate a request has already been resolved
_TERMINAL_STATUSES = {"human_approved", "human_rejected", "auto_approved", "timeout"}


class ApprovalRequest(BaseModel):
    """Request schema for POST /api/v1/requests."""
    agent_id: str
    intent: str
    priority: str
    context_message: str
    metadata_payload: dict[str, Any] | None = None


class ResolveRequest(BaseModel):
    """Request schema for PATCH /api/v1/requests/{id}/resolve."""
    action: str  # "approve" or "reject"
    handled_by: str = "dashboard_user"


# ── POST /api/v1/requests ───────────────────────────────────────────────────

@router.post("/requests")
async def create_request(
    payload: ApprovalRequest,
    wait: bool = Query(False, description="Block until resolved or timeout"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Submit a new human approval request.

    - Persists to database
    - Evaluates decision engine (auto-approve rules)
    - Sends Slack notification if needed
    - If ?wait=true, blocks until human responds or timeout
    """
    request_id = str(uuid.uuid4())

    # ── 1. Persist to DB ──────────────────────────────────────────────────────
    record = HitlRequest(
        id=request_id,
        agent_id=payload.agent_id,
        intent=payload.intent,
        priority=payload.priority,
        metadata_payload=payload.metadata_payload,
        context_message=payload.context_message,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    await db.commit()

    # ── 2. Decision engine (auto-approval rules) ───────────────────────────────
    auto_status = evaluate(payload.intent, payload.metadata_payload)
    if auto_status is not None:
        record.status = auto_status
        record.resolved_at = datetime.now(timezone.utc)
        await db.commit()

        return {
            "status": auto_status,
            "request_id": request_id,
            "message": "Request was automatically approved by policy rules.",
        }

    # ── 3. AI Blast Radius Analysis ──────────────────────────────────────────
    blast_radius = await calculate_blast_radius(
        intent=payload.intent,
        metadata_payload=payload.metadata_payload,
        context_message=payload.context_message,
        agent_id=payload.agent_id,
    )
    record.blast_radius = blast_radius
    await db.commit()

    # ── 4. Send notifications (Slack + Telegram in parallel) ─────────────────
    import asyncio as _aio
    notification_msg = f"{payload.context_message}\n\n{blast_radius}"
    slack_ts, tg_msg_id = await _aio.gather(
        send_slack_message(request_id, notification_msg),
        send_telegram_message(
            request_id, payload.context_message,
            agent_id=payload.agent_id, intent=payload.intent, priority=payload.priority,
            blast_radius=blast_radius,
        ),
    )
    if slack_ts or tg_msg_id:
        if slack_ts:
            record.slack_message_ts = slack_ts
        if tg_msg_id:
            record.telegram_message_id = tg_msg_id
        await db.commit()

    # ── 4. Optionally wait for human response ─────────────────────────────────
    if not wait:
        return {
            "status": "pending",
            "request_id": request_id,
            "message": "Request submitted. Awaiting human approval.",
        }

    # Wait mode: block until resolved or timeout (same as MCP tool)
    event = event_store.register(request_id)
    try:
        await asyncio.wait_for(
            _wait_for_event(event),
            timeout=settings.approval_timeout_seconds,
        )
    except asyncio.TimeoutError:
        # Mark as timeout in DB
        await db.refresh(record)
        if record.status == "pending":
            record.status = "timeout"
            record.resolved_at = datetime.now(timezone.utc)
            await db.commit()

        return {
            "status": "timeout",
            "request_id": request_id,
            "message": "Approval timed out. Request escalated for async resolution.",
        }
    finally:
        event_store.unregister(request_id)

    # Human responded in time — read final status
    await db.refresh(record)
    return {
        "status": record.status,
        "request_id": request_id,
        "handled_by": record.handled_by,
        "message": f"Request {record.status} by {record.handled_by}."
        if record.handled_by
        else f"Request {record.status}.",
    }


async def _wait_for_event(event: asyncio.Event) -> bool:
    await event.wait()
    return True


# ── GET /api/v1/requests ─────────────────────────────────────────────────────

@router.get("/requests")
async def list_requests(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return a paginated list of HITL requests for the audit dashboard."""
    count_stmt = select(func.count()).select_from(HitlRequest)
    if status:
        count_stmt = count_stmt.where(HitlRequest.status == status)
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    stmt = select(HitlRequest).order_by(HitlRequest.created_at.desc())
    if status:
        stmt = stmt.where(HitlRequest.status == status)
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    records = result.scalars().all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "requests": [r.to_dict() for r in records],
    }


# ── GET /api/v1/requests/{id} ────────────────────────────────────────────────

@router.get("/requests/{request_id}")
async def get_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return details for a single HITL request."""
    record: HitlRequest | None = await db.get(HitlRequest, request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return record.to_dict()


# ── PATCH /api/v1/requests/{id}/resolve ───────────────────────────────────────

@router.patch("/requests/{request_id}/resolve")
async def resolve_request(
    request_id: str,
    payload: ResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Approve or reject a pending request from the dashboard."""
    record: HitlRequest | None = await db.get(HitlRequest, request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Request not found")

    if record.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Request already resolved with status '{record.status}'",
        )

    if payload.action == "approve":
        new_status = "human_approved"
    elif payload.action == "reject":
        new_status = "human_rejected"
    else:
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")

    record.status = new_status
    record.handled_by = payload.handled_by
    record.resolved_via = "dashboard"
    record.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)

    # Signal any waiting MCP tool / REST wait coroutine
    event_store.signal(request_id)

    return record.to_dict()


# ── GET /api/v1/trust-scores ──────────────────────────────────────────────────

@router.get("/trust-scores")
async def get_trust_scores(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Compute a trust score (0-100) for each agent based on approval history.

    Formula: starts at 50, +3 per approval, -7 per rejection, clamped 0-100.
    """
    stmt = select(HitlRequest).where(
        HitlRequest.status.in_(["human_approved", "human_rejected", "auto_approved"])
    )
    result = await db.execute(stmt)
    records = result.scalars().all()

    scores: dict[str, dict] = {}
    for r in records:
        if r.agent_id not in scores:
            scores[r.agent_id] = {"score": 50, "approved": 0, "rejected": 0, "total": 0}
        s = scores[r.agent_id]
        s["total"] += 1
        if r.status in ("human_approved", "auto_approved"):
            s["approved"] += 1
            s["score"] = min(100, s["score"] + 3)
        elif r.status == "human_rejected":
            s["rejected"] += 1
            s["score"] = max(0, s["score"] - 7)

    return {"agents": scores}


# ── POST /api/v1/simulate ─────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    intent: str
    metadata_payload: dict[str, Any] | None = None


@router.post("/simulate")
async def simulate_policy(payload: SimulateRequest) -> dict[str, Any]:
    """Simulate the decision engine for a hypothetical request without persisting."""
    canonical = _normalize_intent(payload.intent)
    auto_status = evaluate(payload.intent, payload.metadata_payload)

    if auto_status == "auto_approved":
        verdict = "auto_approved"
        explanation = "This request would be AUTO-APPROVED by policy rules."
    else:
        verdict = "pending"
        explanation = "This request would require HUMAN APPROVAL."

    # Build rule trace
    rules_checked = []
    if canonical == "db_drop":
        rules_checked.append({
            "rule": "Dangerous Operation Block",
            "matched": True,
            "detail": f"Intent '{payload.intent}' normalized to 'db_drop' — always requires human approval.",
        })
    elif canonical == "refund":
        amount = (payload.metadata_payload or {}).get("amount")
        threshold_met = amount is not None and float(amount) < 100
        rules_checked.append({
            "rule": "Low-Value Refund Auto-Approve",
            "matched": threshold_met,
            "detail": f"Amount={amount}, threshold=<100. {'Matched — auto-approved.' if threshold_met else 'Not matched — requires human.'}",
        })
    else:
        rules_checked.append({
            "rule": "Default Policy",
            "matched": False,
            "detail": f"Intent '{payload.intent}' (canonical: '{canonical}') has no auto-approve rule — requires human.",
        })

    return {
        "verdict": verdict,
        "explanation": explanation,
        "canonical_intent": canonical,
        "rules_checked": rules_checked,
    }


# ── POST /api/v1/escalate ─────────────────────────────────────────────────────

@router.post("/escalate")
async def escalate_overdue(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Escalate critical pending requests that exceeded the SLA threshold.

    Called periodically by the frontend or a background job.
    """
    sla_threshold = settings.approval_timeout_seconds * 0.66  # ~30s for 45s timeout
    cutoff = datetime.now(timezone.utc).timestamp() - sla_threshold

    stmt = select(HitlRequest).where(
        HitlRequest.status == "pending",
        HitlRequest.priority == "critical",
    )
    result = await db.execute(stmt)
    records = result.scalars().all()

    escalated_ids = []
    for r in records:
        created_ts = r.created_at.timestamp() if r.created_at else 0
        if created_ts < cutoff:
            r.status = "escalated"
            r.resolved_at = datetime.now(timezone.utc)
            r.handled_by = "auto-escalation"
            escalated_ids.append(r.id)

    if escalated_ids:
        await db.commit()

    return {"escalated": escalated_ids, "count": len(escalated_ids)}

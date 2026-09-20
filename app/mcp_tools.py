"""MCP tool: ``request_human_approval``.

This module registers the tool with the shared MCP server instance and contains
all the business logic described in the project specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from app import events as event_store
from app.config import settings
from app.database import AsyncSessionLocal
from app.decision_engine import evaluate
from app.models import HitlRequest
from app.auditor import calculate_blast_radius
from app.slack import send_slack_message
from app.telegram import send_telegram_message

logger = logging.getLogger(__name__)


def register_tools(server: Server) -> None:
    """Register all MCP tools onto *server*."""

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="request_human_approval",
                description=(
                    "Submit an action for AI governance review. "
                    "High-risk actions are sent to a human approver via Slack; "
                    "low-risk actions may be auto-approved based on policy rules. "
                    "Returns a structured JSON result with the approval status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Identifier of the calling agent.",
                        },
                        "intent": {
                            "type": "string",
                            "description": (
                                "The intended action (e.g. 'refund', 'db_drop')."
                            ),
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "critical"],
                            "description": "Urgency of the request.",
                        },
                        "metadata_payload": {
                            "type": "object",
                            "description": "Arbitrary metadata about the action.",
                        },
                        "context_message": {
                            "type": "string",
                            "description": (
                                "Human-readable explanation shown in the Slack message."
                            ),
                        },
                    },
                    "required": ["agent_id", "intent", "priority", "context_message"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        if name != "request_human_approval":
            raise ValueError(f"Unknown tool: {name}")

        return await _request_human_approval(**arguments)


async def _request_human_approval(
    agent_id: str,
    intent: str,
    priority: str,
    context_message: str,
    metadata_payload: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Core implementation of the ``request_human_approval`` tool."""

    request_id = str(uuid.uuid4())

    # ── 1. Persist to DB ──────────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        record = HitlRequest(
            id=request_id,
            agent_id=agent_id,
            intent=intent,
            priority=priority,
            metadata_payload=metadata_payload,
            context_message=context_message,
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db.add(record)
        await db.commit()

    # ── 2. Decision engine (auto-approval rules) ───────────────────────────────
    auto_status = evaluate(intent, metadata_payload)
    if auto_status is not None:
        async with AsyncSessionLocal() as db:
            record = await db.get(HitlRequest, request_id)
            if record:
                record.status = auto_status
                record.resolved_at = datetime.now(timezone.utc)
                await db.commit()

        result = {
            "status": auto_status,
            "request_id": request_id,
            "message": "Request was automatically approved by policy rules.",
        }
        return [TextContent(type="text", text=json.dumps(result))]

    # ── 3. AI Blast Radius Analysis ──────────────────────────────────────────
    blast_radius = await calculate_blast_radius(
        intent=intent,
        metadata_payload=metadata_payload,
        context_message=context_message,
        agent_id=agent_id,
    )
    async with AsyncSessionLocal() as db:
        record = await db.get(HitlRequest, request_id)
        if record:
            record.blast_radius = blast_radius
            await db.commit()

    # ── 4. Send notifications (Slack + Telegram in parallel) ─────────────────
    notification_msg = f"{context_message}\n\n{blast_radius}"
    slack_ts, tg_msg_id = await asyncio.gather(
        send_slack_message(request_id, notification_msg),
        send_telegram_message(
            request_id, context_message,
            agent_id=agent_id, intent=intent, priority=priority,
            blast_radius=blast_radius,
        ),
    )
    if slack_ts or tg_msg_id:
        async with AsyncSessionLocal() as db:
            record = await db.get(HitlRequest, request_id)
            if record:
                if slack_ts:
                    record.slack_message_ts = slack_ts
                if tg_msg_id:
                    record.telegram_message_id = tg_msg_id
                await db.commit()

    # ── 4. Register async event & wait for human response with timeout ─────────
    event = event_store.register(request_id)

    try:
        resolved_in_time = await asyncio.wait_for(
            _wait_for_event(event),
            timeout=settings.approval_timeout_seconds,
        )
    except asyncio.TimeoutError:
        resolved_in_time = False
    finally:
        event_store.unregister(request_id)

    # ── 5. Handle timeout ─────────────────────────────────────────────────────
    if not resolved_in_time:
        async with AsyncSessionLocal() as db:
            record = await db.get(HitlRequest, request_id)
            if record and record.status == "pending":
                record.status = "timeout"
                record.resolved_at = datetime.now(timezone.utc)
                await db.commit()

        result = {
            "status": "timeout",
            "request_id": request_id,
            "message": "Approval timed out. Request escalated for async resolution.",
        }
        return [TextContent(type="text", text=json.dumps(result))]

    # ── 6. Read final status from DB and return ────────────────────────────────
    async with AsyncSessionLocal() as db:
        record = await db.get(HitlRequest, request_id)

    if record is None:
        result = {
            "status": "error",
            "request_id": request_id,
            "message": "Request record not found after resolution.",
        }
        return [TextContent(type="text", text=json.dumps(result))]

    result = {
        "status": record.status,
        "request_id": request_id,
        "handled_by": f"@{record.handled_by}" if record.handled_by else None,
        "message": (
            f"Request {record.status} by @{record.handled_by}."
            if record.handled_by
            else f"Request {record.status}."
        ),
    }
    return [TextContent(type="text", text=json.dumps(result))]


async def _wait_for_event(event: asyncio.Event) -> bool:
    """Wait for *event* to be set and return True."""
    await event.wait()
    return True

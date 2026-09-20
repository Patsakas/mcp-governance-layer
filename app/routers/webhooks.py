"""Slack webhook receiver: POST /api/v1/webhooks/slack.

Handles interactive component payloads from Slack (button clicks).
The endpoint is idempotent – duplicate deliveries are safely ignored.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import events as event_store
from app.database import get_db
from app.models import HitlRequest
from app.slack import verify_slack_signature, update_slack_message
from app.telegram import answer_callback_query, update_telegram_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

# Statuses that indicate a request has already been resolved
_TERMINAL_STATUSES = {"human_approved", "human_rejected", "auto_approved", "timeout"}


@router.post("/slack")
async def slack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Receive an interactive component payload from Slack."""

    body_bytes = await request.body()

    # ── Security: validate Slack request signature ────────────────────────────
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body_bytes, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # ── Parse the URL-encoded payload sent by Slack ───────────────────────────
    try:
        body_str = body_bytes.decode("utf-8")
        parsed = parse_qs(body_str)
        payload_str = parsed.get("payload", [None])[0]
        if payload_str is None:
            raise ValueError("Missing payload field")
        payload: dict[str, Any] = json.loads(payload_str)
    except Exception as exc:
        logger.warning("Failed to parse Slack payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    # ── Extract the action ────────────────────────────────────────────────────
    actions: list[dict[str, Any]] = payload.get("actions", [])
    if not actions:
        # Acknowledge with 200 but do nothing (e.g. menu open events)
        return Response(status_code=200)

    action = actions[0]
    action_id: str = action.get("action_id", "")
    request_id: str = action.get("value", "")

    # Resolve action_id → status
    if action_id == "approve_action":
        new_status = "human_approved"
    elif action_id == "reject_action":
        new_status = "human_rejected"
    else:
        logger.warning("Unknown action_id '%s' – ignoring", action_id)
        return Response(status_code=200)

    # ── Extract the Slack username of the person who clicked ──────────────────
    user_info = payload.get("user", {})
    handled_by: str = user_info.get("username") or user_info.get("id", "unknown")

    # ── Fetch the DB record ───────────────────────────────────────────────────
    record: HitlRequest | None = await db.get(HitlRequest, request_id)
    if record is None:
        logger.warning("Received Slack action for unknown request ID: %s", request_id)
        return Response(status_code=200)

    # ── Idempotency: ignore if already resolved ───────────────────────────────
    if record.status in _TERMINAL_STATUSES:
        logger.info(
            "Request %s already in terminal status '%s' – ignoring duplicate webhook",
            request_id,
            record.status,
        )
        return Response(status_code=200)

    # ── Update the DB record ──────────────────────────────────────────────────
    record.status = new_status
    record.handled_by = handled_by
    record.resolved_via = "slack"
    record.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)

    # ── Signal the waiting MCP tool ───────────────────────────────────────────
    event_store.signal(request_id)

    # ── Update the Slack message to remove buttons ────────────────────────────
    message_ts: str = (
        payload.get("message", {}).get("ts")
        or payload.get("container", {}).get("message_ts")
        or ""
    )
    context_message: str = _extract_context_message(payload)

    if message_ts:
        await update_slack_message(message_ts, context_message, new_status, handled_by)

    # Return an updated message payload so Slack replaces the original message
    response_payload = {
        "replace_original": True,
        "blocks": _resolved_blocks_simple(new_status, handled_by),
    }
    return Response(
        content=json.dumps(response_payload),
        media_type="application/json",
        status_code=200,
    )


def _extract_context_message(payload: dict[str, Any]) -> str:
    """Best-effort extraction of the original context message from the Slack payload."""
    try:
        blocks = payload.get("message", {}).get("blocks", [])
        for block in blocks:
            if block.get("type") == "section":
                text_obj = block.get("text", {})
                if text_obj.get("type") == "mrkdwn":
                    return text_obj.get("text", "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _resolved_blocks_simple(action: str, handled_by: str) -> list[dict[str, Any]]:
    """Minimal resolved-state blocks returned inline to Slack."""
    emoji = "✅" if action == "human_approved" else "❌"
    label = "Approved" if action == "human_approved" else "Rejected"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{label}* by @{handled_by}",
            },
        }
    ]


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_telegram_callback(request: Request, db: AsyncSession) -> dict[str, str]:
    """Core handler for Telegram callback_query payloads."""
    try:
        data: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.warning("Failed to parse Telegram payload: %s", exc)
        return {"status": "ok"}

    logger.info("Telegram webhook received: %s", list(data.keys()))

    # Only handle callback_query (button presses)
    callback = data.get("callback_query")
    if not callback:
        logger.info("No callback_query in Telegram payload — ignoring")
        return {"status": "ok"}

    callback_id: str = callback.get("id", "")
    callback_data: str = callback.get("data", "")
    tg_user = callback.get("from", {})
    handled_by: str = (
        tg_user.get("username")
        or f'{tg_user.get("first_name", "")} {tg_user.get("last_name", "")}'.strip()
        or str(tg_user.get("id", "unknown"))
    )

    logger.info("Telegram callback: data=%s user=%s", callback_data, handled_by)

    # Parse "approve:REQUEST_ID" or "reject:REQUEST_ID"
    if ":" not in callback_data:
        await answer_callback_query(callback_id, "Invalid action")
        return {"status": "ok"}

    action_str, request_id = callback_data.split(":", 1)

    if action_str == "approve":
        new_status = "human_approved"
    elif action_str == "reject":
        new_status = "human_rejected"
    else:
        await answer_callback_query(callback_id, "Unknown action")
        return {"status": "ok"}

    # ── Answer callback IMMEDIATELY (stops Telegram loading spinner) ──────────
    label = "Approved" if new_status == "human_approved" else "Rejected"
    await answer_callback_query(callback_id, f"{label}!")

    # ── Fetch DB record ──────────────────────────────────────────────────────
    record: HitlRequest | None = await db.get(HitlRequest, request_id)
    if record is None:
        logger.warning("Telegram callback for unknown request ID: %s", request_id)
        return {"status": "ok"}

    # ── Idempotency: ignore if already fully resolved ────────────────────────
    if record.status in _TERMINAL_STATUSES:
        logger.info("Request %s already %s — ignoring", request_id, record.status)
        return {"status": "ok"}

    # ── Update DB (also works for escalated requests) ────────────────────────
    record.status = new_status
    record.handled_by = handled_by
    record.resolved_via = "telegram"
    record.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)

    logger.info("Request %s → %s by %s (via Telegram)", request_id, new_status, handled_by)

    # ── Signal waiting coroutine ─────────────────────────────────────────────
    event_store.signal(request_id)

    # ── Update Telegram message to remove buttons ────────────────────────────
    tg_message = callback.get("message", {})
    tg_message_id = tg_message.get("message_id")
    if tg_message_id:
        try:
            await update_telegram_message(
                tg_message_id,
                new_status,
                handled_by,
                context_message=record.context_message or "",
                intent=record.intent or "",
            )
        except Exception as exc:
            logger.warning("Failed to update Telegram message: %s", exc)

    return {"status": "ok"}


# Both paths so it works regardless of how the user set the webhook URL
@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    return await _handle_telegram_callback(request, db)


@router.post("/telegram/webhook")
async def telegram_webhook_alt(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    return await _handle_telegram_callback(request, db)

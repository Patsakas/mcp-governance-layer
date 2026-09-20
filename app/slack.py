"""Slack integration helpers.

Sends an interactive Block Kit message to the configured channel and provides
helpers for updating the message once a decision has been made.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

from app.config import settings

SLACK_API_BASE = "https://slack.com/api"


# ── Message helpers ───────────────────────────────────────────────────────────

def _approval_blocks(request_id: str, context_message: str) -> list[dict[str, Any]]:
    """Return Slack Block Kit blocks for an approval request."""
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔐 AI Governance: Approval Required",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": context_message,
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"approval_actions_{request_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "reject_action",
                    "value": request_id,
                },
            ],
        },
    ]


def _resolved_blocks(
    context_message: str, action: str, handled_by: str
) -> list[dict[str, Any]]:
    """Return blocks for the resolved (buttons removed) message."""
    emoji = "✅" if action == "human_approved" else "❌"
    label = "Approved" if action == "human_approved" else "Rejected"
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔐 AI Governance: Request Resolved",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": context_message},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{label}* by @{handled_by}",
            },
        },
    ]


# ── Public API ────────────────────────────────────────────────────────────────

async def send_slack_message(request_id: str, context_message: str) -> str | None:
    """Post an interactive approval request to Slack.

    Returns the ``ts`` (timestamp) of the posted message, or ``None`` on failure.
    """
    if not settings.slack_bot_token or not settings.slack_channel_id:
        # Slack not configured; skip silently (useful in tests / local dev).
        return None

    payload: dict[str, Any] = {
        "channel": settings.slack_channel_id,
        "text": f"<!channel> Approval required for request {request_id}",
        "blocks": _approval_blocks(request_id, context_message),
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            headers={
                "Authorization": f"Bearer {settings.slack_bot_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )

    data = response.json()
    if not data.get("ok"):
        # Log error but don't crash the tool
        import logging

        logging.getLogger(__name__).warning(
            "Slack postMessage failed: %s", data.get("error")
        )
        return None

    return data.get("ts")


async def update_slack_message(
    message_ts: str,
    context_message: str,
    action: str,
    handled_by: str,
) -> None:
    """Replace the interactive buttons with a resolution summary."""
    if not settings.slack_bot_token or not settings.slack_channel_id:
        return

    payload: dict[str, Any] = {
        "channel": settings.slack_channel_id,
        "ts": message_ts,
        "text": f"Request resolved by @{handled_by}",
        "blocks": _resolved_blocks(context_message, action, handled_by),
    }

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{SLACK_API_BASE}/chat.update",
            headers={
                "Authorization": f"Bearer {settings.slack_bot_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )


# ── Signature verification ────────────────────────────────────────────────────

def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """Validate a Slack request signature.

    See https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if settings.slack_skip_signature_verification:
        return True

    # Reject requests older than 5 minutes to prevent replay attacks.
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    mac = hmac.new(
        settings.slack_signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    )
    computed = "v0=" + mac.hexdigest()
    return hmac.compare_digest(computed, signature)

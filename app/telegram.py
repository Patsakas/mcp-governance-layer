"""Telegram integration helpers.

Sends an interactive Inline Keyboard message to the configured chat and provides
helpers for updating the message once a decision has been made.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Intent → emoji + app label (mirrors the dashboard INTENT_MAP) ────────────

_INTENT_MAP: dict[str, tuple[str, str]] = {
    "refund":          ("💰", "Finance App"),
    "refund_customer": ("💰", "Finance App"),
    "credit":          ("💳", "Billing App"),
    "issue_credit":    ("💳", "Billing App"),
    "drop_database":   ("🗄️", "Database Console"),
    "db_drop":         ("🗄️", "Database Console"),
    "delete_db":       ("🗄️", "Database Console"),
    "deploy":          ("🚀", "Deploy Service"),
    "restart_service": ("🔄", "Ops Console"),
    "send_email":      ("📧", "Email Service"),
    "delete_user":     ("🚫", "User Service"),
    "create_invoice":  ("🧾", "Invoice App"),
}


def _intent_info(intent: str) -> tuple[str, str]:
    """Return (emoji, app_label) for an intent string."""
    lowered = intent.lower()
    for key, val in _INTENT_MAP.items():
        if key in lowered:
            return val
    return ("⚙️", "MCP Agent")


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/{method}"


# ── Public API ────────────────────────────────────────────────────────────────

async def send_telegram_message(
    request_id: str,
    context_message: str,
    agent_id: str = "",
    intent: str = "",
    priority: str = "",
    blast_radius: str = "",
) -> int | None:
    """Post an interactive approval request to Telegram.

    Returns the ``message_id`` of the posted message, or ``None`` on failure.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return None

    priority_emoji = {"low": "🟢", "medium": "🟡", "critical": "🔴"}.get(priority, "⚪")
    intent_emoji, app_label = _intent_info(intent)

    blast_line = f"\n\n🧠 <b>AI Risk Assessment:</b>\n{_esc(blast_radius)}" if blast_radius else ""

    text = (
        f"{intent_emoji} <b>{_esc(app_label)}: Approval Required</b>\n\n"
        f"🤖 <b>Agent:</b> <code>{_esc(agent_id)}</code>\n"
        f"🎯 <b>Intent:</b> <code>{_esc(intent)}</code>\n"
        f"{priority_emoji} <b>Priority:</b> <code>{_esc(priority)}</code>\n\n"
        f"📋 {_esc(context_message)}"
        f"{blast_line}\n\n"
        f"🆔 <code>{request_id}</code>"
    )

    keyboard: dict[str, Any] = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{request_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{request_id}"},
        ]]
    }

    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _api_url("sendMessage"),
                json=payload,
                timeout=10.0,
            )
        data = response.json()
        if not data.get("ok"):
            logger.warning("Telegram sendMessage failed: %s", data.get("description"))
            return None
        return data.get("result", {}).get("message_id")
    except Exception as exc:
        logger.warning("Telegram sendMessage error: %s", exc)
        return None


async def update_telegram_message(
    message_id: int,
    action: str,
    handled_by: str,
    context_message: str = "",
    intent: str = "",
) -> None:
    """Replace the inline keyboard with a resolution summary."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    emoji = "✅" if action == "human_approved" else "❌"
    label = "Approved" if action == "human_approved" else "Rejected"
    intent_emoji, app_label = _intent_info(intent)

    text = (
        f"{intent_emoji} <b>{_esc(app_label)}: Request Resolved</b>\n\n"
        f"{_esc(context_message)}\n\n"
        f"{emoji} <b>{label}</b> by {_esc(handled_by)}"
    )

    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                _api_url("editMessageText"),
                json=payload,
                timeout=10.0,
            )
    except Exception as exc:
        logger.warning("Telegram editMessageText error: %s", exc)


async def answer_callback_query(callback_query_id: str, text: str) -> None:
    """Send a toast notification to the user who pressed the button."""
    if not settings.telegram_bot_token:
        return

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                _api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=5.0,
            )
    except Exception as exc:
        logger.warning("Telegram answerCallbackQuery error: %s", exc)

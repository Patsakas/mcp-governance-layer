"""AI Auditor — Blast Radius Analysis.

Uses a secondary LLM (OpenAI gpt-4o-mini) to translate raw agent JSON
into a human-readable risk assessment before the approval request
reaches the human approver.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a strict Enterprise Security AI Auditor. "
    "Your ONLY job is to analyze an AI agent's requested action and its JSON payload, "
    "then determine the 'Blast Radius' — the worst-case business/system impact if approved.\n\n"
    "Rules:\n"
    "- Keep your response STRICTLY under 30 words.\n"
    "- Start with the risk level emoji and label.\n"
    "- Be specific about WHAT could go wrong.\n"
    "- Use business language, not technical jargon.\n\n"
    "Format your response EXACTLY like this:\n"
    "🔴 CRITICAL RISK: <explanation>\n"
    "OR\n"
    "🟠 HIGH RISK: <explanation>\n"
    "OR\n"
    "🟡 MEDIUM RISK: <explanation>\n"
    "OR\n"
    "🟢 LOW RISK: <explanation>"
)


async def calculate_blast_radius(
    intent: str,
    metadata_payload: dict[str, Any] | None = None,
    context_message: str = "",
    agent_id: str = "",
) -> str:
    """Call the LLM auditor and return a short blast-radius summary.

    Returns a fallback string if the API key is missing or the call fails.
    """
    if not settings.openai_api_key:
        return _fallback_analysis(intent, metadata_payload)

    user_message = (
        f"Agent: {agent_id}\n"
        f"Intent: {intent}\n"
        f"Context: {context_message}\n"
        f"Payload: {json.dumps(metadata_payload or {})}"
    )

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            max_tokens=80,
        )
        result = response.choices[0].message.content.strip()
        logger.info("Blast radius analysis: %s", result)
        return result
    except Exception as exc:
        logger.warning("Auditor API error: %s", exc)
        return _fallback_analysis(intent, metadata_payload)


def _fallback_analysis(intent: str, metadata_payload: dict[str, Any] | None = None) -> str:
    """Rule-based fallback when OpenAI is unavailable."""
    lowered = intent.lower()

    if "drop" in lowered or "delete_db" in lowered or "db_drop" in lowered:
        return "🔴 CRITICAL RISK: Database destruction could cause permanent data loss and full system outage."

    if "delete_user" in lowered:
        return "🟠 HIGH RISK: User deletion is irreversible and may violate data retention policies."

    if "refund" in lowered or "credit" in lowered:
        amount = (metadata_payload or {}).get("amount")
        if amount is not None:
            try:
                amt = float(amount)
                if amt >= 1000:
                    return f"🟠 HIGH RISK: Large financial operation ({amt} EUR). Potential for significant monetary loss."
                if amt >= 100:
                    return f"🟡 MEDIUM RISK: Refund of {amt} EUR. Verify customer eligibility before approval."
                return f"🟢 LOW RISK: Small refund ({amt} EUR). Within standard auto-approval threshold."
            except (ValueError, TypeError):
                pass
        return "🟡 MEDIUM RISK: Financial operation without specified amount. Verify details before approval."

    if "deploy" in lowered:
        return "🟠 HIGH RISK: Production deployment could introduce bugs or downtime affecting all users."

    if "restart" in lowered:
        return "🟡 MEDIUM RISK: Service restart will cause brief downtime for connected clients."

    if "email" in lowered or "send" in lowered:
        return "🟡 MEDIUM RISK: Email sending is irreversible. Verify recipients and content before approval."

    return "🟡 MEDIUM RISK: Action requires human review. Evaluate context before approving."

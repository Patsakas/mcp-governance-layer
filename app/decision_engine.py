"""Decision engine: determines whether a request can be auto-approved."""

from __future__ import annotations

from typing import Any


def _normalize_intent(intent: str) -> str:
    """Normalize intent to a canonical keyword for rule matching.

    Maps variants like 'refund_customer', 'drop_database', 'db_drop' to
    canonical forms so the decision engine doesn't miss matches.
    """
    lowered = intent.lower().strip()

    # Database-destructive intents
    if "drop" in lowered or "delete_db" in lowered or "db_drop" in lowered:
        return "db_drop"

    # Refund intents
    if "refund" in lowered:
        return "refund"

    return lowered


def evaluate(intent: str, metadata_payload: dict[str, Any] | None) -> str | None:
    """Return the auto-resolved status string, or ``None`` if human approval is needed.

    Rules (evaluated in priority order):
    1. Intent contains "drop" / "db_drop" → always requires human approval.
    2. Intent contains "refund" AND ``metadata_payload["amount"] < 100``
       → auto-approved.

    All other cases return ``None`` (human approval required).
    """
    canonical = _normalize_intent(intent)

    if canonical == "db_drop":
        # Always require human approval – never auto-approve.
        return None

    if canonical == "refund":
        amount = (metadata_payload or {}).get("amount")
        try:
            if amount is not None and float(amount) < 100:
                return "auto_approved"
        except (TypeError, ValueError):
            pass

    return None

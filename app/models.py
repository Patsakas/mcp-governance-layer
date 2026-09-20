"""SQLAlchemy ORM model for the HitlRequest audit table."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base


class HitlRequest(Base):
    """Represents a single human-in-the-loop approval request."""

    __tablename__ = "hitl_requests"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    metadata_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
    )
    context_message: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    slack_message_ts: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    handled_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    resolved_via: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    blast_radius: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "intent": self.intent,
            "priority": self.priority,
            "metadata_payload": self.metadata_payload,
            "context_message": self.context_message,
            "status": self.status,
            "handled_by": self.handled_by,
            "resolved_via": self.resolved_via,
            "blast_radius": self.blast_radius,
            "notified_slack": self.slack_message_ts is not None,
            "notified_telegram": self.telegram_message_id is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

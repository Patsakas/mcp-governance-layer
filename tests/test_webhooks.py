"""Tests for the Slack webhook receiver."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, urlencode

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models import HitlRequest

# ── Shared in-memory test database ───────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="function")
async def db_session():
    """Provide a fresh in-memory DB session for each test function."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_client(db_session):
    """Provide an async test HTTP client wired to the test database."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_slack_payload(request_id: str, action_id: str, username: str = "johndoe") -> str:
    """Return a URL-encoded Slack interactive payload string."""
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": username},
        "actions": [
            {
                "action_id": action_id,
                "value": request_id,
            }
        ],
        "message": {
            "ts": "1234567890.123456",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Test context message"},
                }
            ],
        },
        "container": {"message_ts": "1234567890.123456"},
    }
    return urlencode({"payload": json.dumps(payload)})


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slack_webhook_approve(async_client, db_session):
    """Approve action should set status to 'human_approved'."""
    request_id = "test-request-001"
    record = HitlRequest(
        id=request_id,
        agent_id="agent-1",
        intent="db_drop",
        priority="critical",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(record)
    await db_session.commit()

    from app import events as event_store

    event = event_store.register(request_id)

    with patch("app.routers.webhooks.update_slack_message", new_callable=AsyncMock):
        response = await async_client.post(
            "/api/v1/webhooks/slack",
            content=_make_slack_payload(request_id, "approve_action"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": "0",
                "X-Slack-Signature": "v0=bypass",
            },
        )

    assert response.status_code == 200

    await db_session.refresh(record)
    assert record.status == "human_approved"
    assert record.handled_by == "johndoe"
    assert record.resolved_at is not None
    event_store.unregister(request_id)


@pytest.mark.asyncio
async def test_slack_webhook_reject(async_client, db_session):
    """Reject action should set status to 'human_rejected'."""
    request_id = "test-request-002"
    record = HitlRequest(
        id=request_id,
        agent_id="agent-1",
        intent="db_drop",
        priority="critical",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(record)
    await db_session.commit()

    from app import events as event_store

    event_store.register(request_id)

    with patch("app.routers.webhooks.update_slack_message", new_callable=AsyncMock):
        response = await async_client.post(
            "/api/v1/webhooks/slack",
            content=_make_slack_payload(request_id, "reject_action"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": "0",
                "X-Slack-Signature": "v0=bypass",
            },
        )

    assert response.status_code == 200

    await db_session.refresh(record)
    assert record.status == "human_rejected"
    event_store.unregister(request_id)


@pytest.mark.asyncio
async def test_slack_webhook_idempotent(async_client, db_session):
    """A duplicate webhook for an already-resolved request must be ignored."""
    request_id = "test-request-003"
    record = HitlRequest(
        id=request_id,
        agent_id="agent-1",
        intent="db_drop",
        priority="critical",
        status="human_approved",
        handled_by="alice",
        created_at=datetime.now(timezone.utc),
        resolved_at=datetime.now(timezone.utc),
    )
    db_session.add(record)
    await db_session.commit()

    with patch("app.routers.webhooks.update_slack_message", new_callable=AsyncMock):
        response = await async_client.post(
            "/api/v1/webhooks/slack",
            content=_make_slack_payload(request_id, "reject_action", username="bob"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": "0",
                "X-Slack-Signature": "v0=bypass",
            },
        )

    # Should still return 200 but leave the record unchanged
    assert response.status_code == 200
    await db_session.refresh(record)
    assert record.status == "human_approved"  # unchanged
    assert record.handled_by == "alice"  # unchanged


@pytest.mark.asyncio
async def test_slack_webhook_unknown_request_id(async_client, db_session):
    """Webhook for an unknown request ID must return 200 (graceful ignore)."""
    with patch("app.routers.webhooks.update_slack_message", new_callable=AsyncMock):
        response = await async_client.post(
            "/api/v1/webhooks/slack",
            content=_make_slack_payload("nonexistent-id", "approve_action"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": "0",
                "X-Slack-Signature": "v0=bypass",
            },
        )
    assert response.status_code == 200

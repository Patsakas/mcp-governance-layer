"""Tests for the audit / dashboard request endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models import HitlRequest

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="function")
async def db_session():
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
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


async def _seed_requests(db: AsyncSession, count: int = 3) -> list[HitlRequest]:
    records = []
    for i in range(count):
        r = HitlRequest(
            id=f"req-{i:03d}",
            agent_id=f"agent-{i}",
            intent="refund",
            priority="low",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db.add(r)
        records.append(r)
    await db.commit()
    return records


@pytest.mark.asyncio
async def test_list_requests_empty(async_client):
    response = await async_client.get("/api/v1/requests")
    assert response.status_code == 200
    data = response.json()
    assert data["requests"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_requests_returns_all(async_client, db_session):
    await _seed_requests(db_session, 3)
    response = await async_client.get("/api/v1/requests")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["requests"]) == 3


@pytest.mark.asyncio
async def test_list_requests_filter_by_status(async_client, db_session):
    records = await _seed_requests(db_session, 3)
    # Mark first record as approved
    records[0].status = "human_approved"
    await db_session.commit()

    response = await async_client.get("/api/v1/requests?status=human_approved")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["requests"][0]["status"] == "human_approved"


@pytest.mark.asyncio
async def test_get_single_request(async_client, db_session):
    records = await _seed_requests(db_session, 1)
    request_id = records[0].id

    response = await async_client.get(f"/api/v1/requests/{request_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == request_id
    assert data["intent"] == "refund"


@pytest.mark.asyncio
async def test_get_single_request_not_found(async_client):
    response = await async_client.get("/api/v1/requests/nonexistent-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_requests_pagination(async_client, db_session):
    await _seed_requests(db_session, 5)

    response = await async_client.get("/api/v1/requests?limit=2&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert len(data["requests"]) == 2

    response2 = await async_client.get("/api/v1/requests?limit=2&offset=2")
    data2 = response2.json()
    assert len(data2["requests"]) == 2

    # IDs in first page should differ from second page
    ids_page1 = {r["id"] for r in data["requests"]}
    ids_page2 = {r["id"] for r in data2["requests"]}
    assert ids_page1.isdisjoint(ids_page2)

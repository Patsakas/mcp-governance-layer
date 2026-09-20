"""FastAPI application entry point.

Exposes:
- MCP SSE transport:  GET  /sse
                      POST /messages
- REST API:           POST /api/v1/webhooks/slack
                      GET  /api/v1/requests
                      GET  /api/v1/requests/{id}
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcp.server import Server
from mcp.server.sse import SseServerTransport

from app.database import init_db
from app.mcp_tools import register_tools
from app.routers import requests as requests_router
from app.routers import webhooks as webhooks_router

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent

# ── MCP Server ────────────────────────────────────────────────────────────────

mcp_server = Server("mcp-governance-layer")
register_tools(mcp_server)

sse_transport = SseServerTransport("/messages")


# ── FastAPI lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    """Application lifespan: initialise the database on startup."""
    await init_db()
    logger.info("Database initialised.")
    yield


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="MCP Governance Layer",
    description=(
        "Human-in-the-Loop (HITL) governance layer for autonomous AI systems "
        "using the Model Context Protocol (MCP)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# REST API routers
app.include_router(webhooks_router.router)
app.include_router(requests_router.router)


# ── MCP SSE endpoints ─────────────────────────────────────────────────────────
# These are implemented as pure ASGI handlers so the transport's `connect_sse`
# and `handle_post_message` receive the raw (scope, receive, send) triple
# without needing to access any private attributes on Starlette's Request.

async def _handle_sse(scope: Scope, receive: Receive, send: Send) -> None:
    """MCP SSE connection endpoint (GET /sse).

    Clients connect here to establish an SSE stream; the server sends back the
    URL they should POST messages to.
    """
    async with sse_transport.connect_sse(scope, receive, send) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )
    # Send an empty response after the SSE session ends to satisfy the ASGI contract.
    await Response()(scope, receive, send)


async def _handle_messages(scope: Scope, receive: Receive, send: Send) -> None:
    """MCP message POST endpoint (POST /messages).

    Clients POST JSON-RPC messages here after establishing an SSE connection.
    The transport writes its own response directly into the ASGI send callable.
    """
    await sse_transport.handle_post_message(scope, receive, send)


# Register MCP routes directly on the underlying Starlette router so they
# participate in the FastAPI routing table (and show up in /docs).
app.add_route("/sse", _handle_sse, methods=["GET"], include_in_schema=True)
app.add_route("/messages", _handle_messages, methods=["POST"], include_in_schema=True)


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    """Simple health-check endpoint."""
    return {"status": "ok", "service": "mcp-governance-layer"}


@app.get("/")
async def dashboard() -> HTMLResponse:
    """Serve the dashboard UI."""
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


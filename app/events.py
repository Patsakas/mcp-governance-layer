"""In-memory registry of asyncio.Event objects keyed by request ID.

When the MCP tool is waiting for human approval it registers an event here.
The Slack webhook handler looks up the event by request ID and sets it,
which unblocks the waiting tool coroutine.
"""

from __future__ import annotations

import asyncio
from typing import Optional

_events: dict[str, asyncio.Event] = {}


def register(request_id: str) -> asyncio.Event:
    """Create and register a new event for *request_id*."""
    event = asyncio.Event()
    _events[request_id] = event
    return event


def get(request_id: str) -> Optional[asyncio.Event]:
    """Return the event for *request_id*, or ``None`` if not registered."""
    return _events.get(request_id)


def signal(request_id: str) -> bool:
    """Set the event for *request_id*.  Returns True if the event existed."""
    event = _events.get(request_id)
    if event is not None:
        event.set()
        return True
    return False


def unregister(request_id: str) -> None:
    """Remove the event for *request_id* (called after the tool returns)."""
    _events.pop(request_id, None)

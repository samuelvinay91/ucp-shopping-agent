"""SSE streaming manager for real-time shopping workflow updates.

Provides an event bus that the orchestrator nodes write to and that API
endpoints consume via ``async for`` iteration.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import structlog

from ucp_shopping.models import ShoppingEvent

logger = structlog.get_logger(__name__)

# Canonical event type constants
EVENT_PLANNING = "planning"
EVENT_MERCHANTS_DISCOVERED = "merchants_discovered"
EVENT_SEARCHING = "searching"
EVENT_PRODUCTS_FOUND = "products_found"
EVENT_COMPARING = "comparing"
EVENT_COMPARISON_READY = "comparison_ready"
EVENT_OPTIMIZING = "optimizing"
EVENT_OPTIMIZATION_READY = "optimization_ready"
EVENT_AWAITING_CONFIRMATION = "awaiting_confirmation"
EVENT_CHECKING_OUT = "checking_out"
EVENT_CHECKOUT_PROGRESS = "checkout_progress"
EVENT_COMPLETED = "completed"
EVENT_ERROR = "error"


class ShoppingEventStream:
    """In-memory pub/sub for shopping session SSE events.

    Each shopping session gets its own ``asyncio.Queue`` so that multiple
    SSE subscribers can consume events independently.
    """

    def __init__(self, max_queue_size: int = 256) -> None:
        self._queues: dict[str, list[asyncio.Queue[ShoppingEvent | None]]] = {}
        self._max_queue_size = max_queue_size
        self._history: dict[str, list[ShoppingEvent]] = {}

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def emit(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        message: str = "",
    ) -> ShoppingEvent:
        """Push an event to all subscribers of *session_id*.

        Returns the constructed :class:`ShoppingEvent` for convenience.
        """
        event = ShoppingEvent(
            event_type=event_type,
            session_id=session_id,
            data=data or {},
            message=message,
            timestamp=datetime.now(tz=timezone.utc),
        )

        # Persist in history
        self._history.setdefault(session_id, []).append(event)

        # Fan-out to all live subscriber queues
        queues = self._queues.get(session_id, [])
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "event_queue_full",
                    session_id=session_id,
                    event_type=event_type,
                )

        logger.debug(
            "event_emitted",
            session_id=session_id,
            event_type=event_type,
            subscribers=len(queues),
        )
        return event

    # ------------------------------------------------------------------
    # Subscribing
    # ------------------------------------------------------------------

    async def subscribe(self, session_id: str) -> AsyncIterator[ShoppingEvent]:
        """Yield events for *session_id* as they arrive.

        The iterator terminates when the session emits a ``completed`` or
        ``error`` event, or when ``close(session_id)`` is called (which
        pushes ``None`` as a sentinel).
        """
        queue: asyncio.Queue[ShoppingEvent | None] = asyncio.Queue(
            maxsize=self._max_queue_size
        )
        self._queues.setdefault(session_id, []).append(queue)

        # Replay any historical events first so late joiners catch up
        for past_event in self._history.get(session_id, []):
            yield past_event

        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Sentinel: stream closed
                    break
                yield event
                if event.event_type in (EVENT_COMPLETED, EVENT_ERROR):
                    break
        finally:
            # Clean up this subscriber
            session_queues = self._queues.get(session_id, [])
            if queue in session_queues:
                session_queues.remove(queue)

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def close(self, session_id: str) -> None:
        """Signal all subscribers of *session_id* to stop iterating."""
        for queue in self._queues.get(session_id, []):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._queues.pop(session_id, None)

    def get_history(self, session_id: str) -> list[ShoppingEvent]:
        """Return all events emitted for a given session."""
        return list(self._history.get(session_id, []))

    def clear(self, session_id: str) -> None:
        """Remove all state associated with a session."""
        self.close(session_id)
        self._history.pop(session_id, None)

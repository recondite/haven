"""In-memory pub/sub event bus that fans out to SSE subscribers."""
import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, event: str, data: Any) -> None:
        payload = {"event": event, "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer — drop the event rather than block producers.
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


bus = EventBus()

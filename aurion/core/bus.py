"""Single-consumer asyncio event bus.

One loop processing events in order. There is no cross-process broker: at this scale
(7 instruments, ~1 evaluation per minute at worst) a broker is pure overhead and an extra
failure mode (`docs/01-architecture.md` §5).

Ordering matters for correctness, not just tidiness. A quote that arrives out of order
relative to a bar close must not let the engine observe a spread from `t+1` while
evaluating `t`, so the bus preserves submission order and handlers run to completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_log = logging.getLogger(__name__)

E = TypeVar("E")
Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    __slots__ = ("_handlers", "_processed", "_queue", "_running", "_task")

    def __init__(self, maxsize: int = 10_000) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._processed = 0

    def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: object) -> None:
        await self._queue.put(event)

    async def drain(self) -> None:
        """Process everything currently queued. The backtest driver uses this directly."""
        while not self._queue.empty():
            await self._dispatch(self._queue.get_nowait())

    async def _dispatch(self, event: object) -> None:
        for handler in self._handlers.get(type(event), ()):
            try:
                await handler(event)
            except Exception:
                # A handler raising must not stop the loop or drop later events; the
                # supervisor surfaces this as a health alert.
                _log.exception("handler failed", extra={"event": type(event).__name__})
        self._processed += 1

    async def run(self) -> None:
        self._running = True
        while self._running:
            event = await self._queue.get()
            await self._dispatch(event)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def processed(self) -> int:
        return self._processed

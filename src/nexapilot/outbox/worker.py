from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from nexapilot.log import logger
from nexapilot.model import OutboxEvent
from nexapilot.store.sqlite import SQLiteStore


OutboxHandler = Callable[[OutboxEvent], Awaitable[None]]


class OutboxWorker:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        worker_id: str,
        handlers: dict[str, OutboxHandler],
        interval_ms: int = 1_000,
        max_attempts: int = 5,
        batch_size: int = 10,
        lease_timeout_ms: int = 60_000,
        retry_base_ms: int = 1_000,
    ) -> None:
        self._store = store
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._interval_ms = max(100, interval_ms)
        self._max_attempts = max(1, max_attempts)
        self._batch_size = max(1, batch_size)
        self._lease_timeout_ms = max(1_000, lease_timeout_ms)
        self._retry_base_ms = max(100, retry_base_ms)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._log = logger.child(service="outbox.worker", worker_id=worker_id)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name=f"outbox:{self._worker_id}")
        self._log.info("Outbox worker started", event="outbox.worker.started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._log.info("Outbox worker stopped", event="outbox.worker.stopped")

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                processed = 0
                self._log.error(
                    "Outbox worker tick failed",
                    event="outbox.worker.tick_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if processed == 0:
                await asyncio.sleep(self._interval_ms / 1_000)

    async def run_once(self, *, now_ms: int | None = None) -> int:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        events = await self._store.claim_outbox_events(
            worker_id=self._worker_id,
            limit=self._batch_size,
            now_ms=now,
            lease_timeout_ms=self._lease_timeout_ms,
            event_types=set(self._handlers),
        )
        for event in events:
            await self._process(event, now_ms=now)
        return len(events)

    async def _process(self, event: OutboxEvent, *, now_ms: int) -> None:
        handler = self._handlers.get(event.event_type)
        try:
            if handler is None:
                raise RuntimeError(f"no outbox handler registered for {event.event_type}")
            await handler(event)
            await self._store.mark_outbox_processed(event.id, worker_id=self._worker_id, now_ms=now_ms)
            self._log.info(
                "Outbox event processed",
                event="outbox.event.processed",
                event_id=event.id,
                event_type=event.event_type,
                attempts=event.attempts,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = self._retry_base_ms * (2 ** max(event.attempts - 1, 0))
            failed = await self._store.mark_outbox_failed(
                event.id,
                worker_id=self._worker_id,
                error=f"{type(exc).__name__}: {exc}",
                max_attempts=self._max_attempts,
                next_retry_at=now_ms + delay,
            )
            self._log.warning(
                "Outbox event processing failed",
                event="outbox.event.failed",
                event_id=event.id,
                event_type=event.event_type,
                status=failed.status,
                attempts=failed.attempts,
                error=str(exc),
            )

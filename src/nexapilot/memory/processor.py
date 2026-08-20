from __future__ import annotations

from collections.abc import Awaitable, Callable

from nexapilot.model import MessageWithParts, OutboxEvent
from nexapilot.store.sqlite import SQLiteStore


MemoryBatchHandler = Callable[[OutboxEvent, list[MessageWithParts]], Awaitable[None]]


class IncrementalMemoryProcessor:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        processor_name: str,
        handler: MemoryBatchHandler,
    ) -> None:
        if not processor_name.strip():
            raise ValueError("memory processor name is required")
        self._store = store
        self._processor_name = processor_name
        self._handler = handler

    async def __call__(self, event: OutboxEvent) -> None:
        if event.event_type != "run.completed":
            raise ValueError(f"unsupported memory event type: {event.event_type}")
        if event.session_id is None or event.sequence_from is None or event.sequence_to is None:
            raise ValueError("run.completed event requires session and sequence range")

        checkpoint = await self._store.get_memory_checkpoint(self._processor_name, event.session_id)
        if event.sequence_to <= checkpoint.last_message_sequence:
            return
        expected_sequence = checkpoint.last_message_sequence + 1
        if event.sequence_from > expected_sequence:
            raise RuntimeError(
                f"memory event sequence gap for {event.session_id}: "
                f"expected {expected_sequence}, got {event.sequence_from}"
            )
        messages = await self._store.list_messages(
            event.session_id,
            after_sequence=checkpoint.last_message_sequence,
            through_sequence=event.sequence_to,
        )
        await self._handler(event, messages)
        await self._store.advance_memory_checkpoint(
            processor_name=self._processor_name,
            session_id=event.session_id,
            last_message_sequence=event.sequence_to,
        )

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.memory import IncrementalMemoryProcessor
from nexapilot.model import Message, ModelRef, OutboxEvent, Session
from nexapilot.store.sqlite import SQLiteStore


class IncrementalMemoryProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def _create_store(self, directory: str) -> SQLiteStore:
        store = SQLiteStore(str(Path(directory) / "test.sqlite3"))
        await store.init()
        await store.create_session(
            Session(
                id="session-a",
                title="test",
                worktree=directory,
                cwd=directory,
                created_at=1,
                updated_at=1,
                permission_rules=[],
            )
        )
        model = ModelRef(provider="openai-compatible", id="test-model")
        for sequence in range(1, 7):
            message = await store.add_message(
                Message(
                    id=f"message-{sequence}",
                    session_id="session-a",
                    role="user" if sequence % 2 else "assistant",
                    agent="primary",
                    model=model,
                    created_at=sequence,
                )
            )
            self.assertEqual(message.sequence, sequence)
        return store

    @staticmethod
    def _event(sequence_from: int, sequence_to: int) -> OutboxEvent:
        return OutboxEvent(
            id=f"event-{sequence_to}",
            idempotency_key=f"run.completed:run-{sequence_to}",
            event_type="run.completed",
            aggregate_type="run",
            aggregate_id=f"run-{sequence_to}",
            session_id="session-a",
            run_id=f"run-{sequence_to}",
            sequence_from=sequence_from,
            sequence_to=sequence_to,
            payload={},
            created_at=sequence_to,
        )

    async def test_processes_only_unseen_messages_across_session_switches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = await self._create_store(tmp)
            batches: list[list[str]] = []

            async def capture(_event, messages):
                batches.append([message.info.id for message in messages])

            processor = IncrementalMemoryProcessor(
                store=store,
                processor_name="episodic",
                handler=capture,
            )
            events = [self._event(1, 2), self._event(3, 4), self._event(5, 6)]
            for event in events:
                await processor(event)
            await processor(events[0])

            self.assertEqual(
                batches,
                [
                    ["message-1", "message-2"],
                    ["message-3", "message-4"],
                    ["message-5", "message-6"],
                ],
            )
            checkpoint = await store.get_memory_checkpoint("episodic", "session-a")
            self.assertEqual(checkpoint.last_message_sequence, 6)

    async def test_handler_failure_does_not_advance_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = await self._create_store(tmp)

            async def fail(_event, _messages):
                raise RuntimeError("extraction failed")

            processor = IncrementalMemoryProcessor(
                store=store,
                processor_name="episodic",
                handler=fail,
            )
            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                await processor(self._event(1, 2))

            checkpoint = await store.get_memory_checkpoint("episodic", "session-a")
            self.assertEqual(checkpoint.last_message_sequence, 0)
            self.assertEqual(checkpoint.updated_at, 0)

    async def test_out_of_order_event_is_retried_without_skipping_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = await self._create_store(tmp)
            handled: list[str] = []

            async def capture(_event, messages):
                handled.extend(message.info.id for message in messages)

            processor = IncrementalMemoryProcessor(
                store=store,
                processor_name="episodic",
                handler=capture,
            )
            with self.assertRaisesRegex(RuntimeError, "expected 1, got 3"):
                await processor(self._event(3, 4))

            self.assertEqual(handled, [])
            checkpoint = await store.get_memory_checkpoint("episodic", "session-a")
            self.assertEqual(checkpoint.last_message_sequence, 0)

    async def test_checkpoint_never_regresses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = await self._create_store(tmp)
            first = await store.advance_memory_checkpoint(
                processor_name="episodic",
                session_id="session-a",
                last_message_sequence=5,
                now_ms=100,
            )
            second = await store.advance_memory_checkpoint(
                processor_name="episodic",
                session_id="session-a",
                last_message_sequence=3,
                now_ms=200,
            )

            self.assertEqual(first.last_message_sequence, 5)
            self.assertEqual(second.last_message_sequence, 5)
            self.assertEqual(second.updated_at, 100)


if __name__ == "__main__":
    unittest.main()

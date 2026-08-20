from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from nexapilot.model import Message, ModelRef, Session
from nexapilot.store.sqlite import SQLiteStore


class OutboxStoreTests(unittest.IsolatedAsyncioTestCase):
    async def _create_running_run(self, store: SQLiteStore, worktree: str):
        await store.create_session(
            Session(
                id="session-1",
                title="test",
                worktree=worktree,
                cwd=worktree,
                created_at=1,
                updated_at=1,
                permission_rules=[],
            )
        )
        model = ModelRef(provider="openai-compatible", id="test-model")
        user = await store.add_message(
            Message(
                id="user-1",
                session_id="session-1",
                role="user",
                agent="primary",
                model=model,
                created_at=1,
            )
        )
        run = await store.create_run(
            session_id="session-1",
            trigger_message_id=user.id,
            source="api",
            agent_name="primary",
            model=model,
        )
        user_sequence = await store.attach_message_to_run(user.id, run.id)
        run = await store.start_run(run.id, input_sequence=user_sequence)
        assistant = await store.add_message(
            Message(
                id="assistant-1",
                session_id="session-1",
                run_id=run.id,
                role="assistant",
                parent_id=user.id,
                agent="primary",
                model=model,
                created_at=2,
            )
        )
        return run, assistant

    async def test_enqueue_is_idempotent_and_preserves_message_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()

            first = await store.enqueue_outbox_event(
                idempotency_key="run.completed:run-1",
                event_type="run.completed",
                aggregate_type="run",
                aggregate_id="run-1",
                session_id="session-1",
                run_id="run-1",
                sequence_from=10,
                sequence_to=14,
                payload={"finish_reason": "stop"},
                now_ms=100,
            )
            duplicate = await store.enqueue_outbox_event(
                idempotency_key="run.completed:run-1",
                event_type="run.completed",
                aggregate_type="run",
                aggregate_id="run-1",
                session_id="session-1",
                run_id="run-1",
                sequence_from=10,
                sequence_to=14,
                payload={"finish_reason": "stop"},
                now_ms=200,
            )

            self.assertEqual(duplicate.id, first.id)
            self.assertEqual(first.status, "pending")
            self.assertEqual(first.sequence_from, 10)
            self.assertEqual(first.sequence_to, 14)
            self.assertEqual(first.created_at, 100)
            self.assertEqual(await store.get_outbox_event(first.id), first)
            self.assertEqual(await store.list_outbox_events(status="pending"), [first])

    async def test_idempotency_key_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await store.enqueue_outbox_event(
                idempotency_key="stable-key",
                event_type="run.completed",
                aggregate_type="run",
                aggregate_id="run-1",
                payload={"value": 1},
            )

            with self.assertRaisesRegex(ValueError, "collision"):
                await store.enqueue_outbox_event(
                    idempotency_key="stable-key",
                    event_type="run.completed",
                    aggregate_type="run",
                    aggregate_id="run-1",
                    payload={"value": 2},
                )

            self.assertEqual(len(await store.list_outbox_events()), 1)

    async def test_outbox_conflict_rolls_back_message_and_run_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            run, assistant = await self._create_running_run(store, tmp)
            await store.enqueue_outbox_event(
                idempotency_key=f"run.completed:{run.id}",
                event_type="injected.conflict",
                aggregate_type="run",
                aggregate_id=run.id,
                payload={"injected": True},
            )

            with self.assertRaises(sqlite3.IntegrityError):
                await store.finish_assistant_run_with_outbox(
                    assistant_message_id=assistant.id,
                    completed_at=100,
                    finish_reason="stop",
                    run_status="completed",
                )

            unchanged_run = await store.get_run(run.id)
            self.assertEqual(unchanged_run.status, "running")
            self.assertIsNone(unchanged_run.completed_at)
            history = await store.list_messages("session-1")
            unchanged_assistant = next(message.info for message in history if message.info.id == assistant.id)
            self.assertIsNone(unchanged_assistant.completed_at)
            self.assertIsNone(unchanged_assistant.finish)


if __name__ == "__main__":
    unittest.main()

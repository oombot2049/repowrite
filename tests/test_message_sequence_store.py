from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from nexapilot.model import Message, ModelRef, Session
from nexapilot.store.sqlite import SQLiteStore


class MessageSequenceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def _create_session(self, store: SQLiteStore, session_id: str, worktree: str) -> None:
        await store.create_session(
            Session(
                id=session_id,
                title=session_id,
                worktree=worktree,
                cwd=worktree,
                created_at=1,
                updated_at=1,
                permission_rules=[],
            )
        )

    @staticmethod
    def _message(*, message_id: str, session_id: str, run_id: str | None = None) -> Message:
        return Message(
            id=message_id,
            session_id=session_id,
            run_id=run_id,
            role="user",
            agent="primary",
            model=ModelRef(provider="openai-compatible", id="test-model"),
            created_at=1,
        )

    async def test_message_sequence_is_atomic_and_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)

            messages = await asyncio.gather(
                *(store.add_message(self._message(message_id=f"m-{index}", session_id="session-a")) for index in range(5))
            )

            self.assertEqual(sorted(message.sequence for message in messages), [1, 2, 3, 4, 5])
            history = await store.list_messages("session-a")
            self.assertEqual([message.info.sequence for message in history], [1, 2, 3, 4, 5])

    async def test_messages_can_be_filtered_by_run_and_sequence_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)
            model = ModelRef(provider="openai-compatible", id="test-model")
            first_run = await store.create_run(
                session_id="session-a",
                trigger_message_id=None,
                source="api",
                agent_name="primary",
                model=model,
            )
            second_run = await store.create_run(
                session_id="session-a",
                trigger_message_id=None,
                source="api",
                agent_name="primary",
                model=model,
            )

            await store.add_message(self._message(message_id="m-1", session_id="session-a", run_id=first_run.id))
            await store.add_message(self._message(message_id="m-2", session_id="session-a", run_id=first_run.id))
            await store.add_message(self._message(message_id="m-3", session_id="session-a", run_id=second_run.id))
            await store.add_message(self._message(message_id="m-4", session_id="session-a", run_id=second_run.id))

            second_run_messages = await store.list_messages("session-a", run_id=second_run.id)
            self.assertEqual([message.info.id for message in second_run_messages], ["m-3", "m-4"])

            range_messages = await store.list_messages("session-a", after_sequence=1, through_sequence=3)
            self.assertEqual([message.info.id for message in range_messages], ["m-2", "m-3"])

    async def test_message_cannot_reference_run_from_another_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)
            await self._create_session(store, "session-b", tmp)
            run = await store.create_run(
                session_id="session-a",
                trigger_message_id=None,
                source="api",
                agent_name="primary",
                model=ModelRef(provider="openai-compatible", id="test-model"),
            )

            with self.assertRaisesRegex(ValueError, "same session"):
                await store.add_message(
                    self._message(message_id="m-1", session_id="session-b", run_id=run.id)
                )

            self.assertEqual(await store.list_messages("session-b"), [])


if __name__ == "__main__":
    unittest.main()

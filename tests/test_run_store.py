from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from nexapilot.model import ModelRef, Session
from nexapilot.store.sqlite import SQLiteStore


class RunStoreTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_runs_receive_monotonic_sequence_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)
            model = ModelRef(provider="openai-compatible", id="test-model")

            first = await store.create_run(
                session_id="session-a",
                trigger_message_id="message-1",
                source="api",
                agent_name="primary",
                model=model,
                now_ms=100,
            )
            second = await store.create_run(
                session_id="session-a",
                trigger_message_id="message-2",
                source="api",
                agent_name="primary",
                model=model,
                now_ms=200,
            )

            self.assertEqual(first.sequence, 1)
            self.assertEqual(second.sequence, 2)
            self.assertEqual(first.status, "queued")
            self.assertEqual(first.trigger_message_id, "message-1")
            self.assertEqual(first.model, model)
            self.assertEqual(first.created_at, 100)
            self.assertEqual(first.updated_at, 100)

            listed = await store.list_runs("session-a")
            self.assertEqual([run.sequence for run in listed], [2, 1])
            self.assertEqual(await store.get_run(first.id), first)

            with self.assertRaisesRegex(KeyError, "assistant message"):
                await store.get_run_by_assistant_message("not-finished")

    async def test_run_sequence_is_isolated_by_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)
            await self._create_session(store, "session-b", tmp)
            model = ModelRef(provider="openai-compatible", id="test-model")

            run_a = await store.create_run(
                session_id="session-a",
                trigger_message_id=None,
                source="api",
                agent_name="primary",
                model=model,
            )
            run_b = await store.create_run(
                session_id="session-b",
                trigger_message_id=None,
                source="cron",
                agent_name="primary",
                model=model,
            )

            self.assertEqual(run_a.sequence, 1)
            self.assertEqual(run_b.sequence, 1)
            self.assertEqual(run_b.source, "cron")

    async def test_concurrent_run_creation_keeps_sequence_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)
            model = ModelRef(provider="openai-compatible", id="test-model")

            runs = await asyncio.gather(
                *(
                    store.create_run(
                        session_id="session-a",
                        trigger_message_id=f"message-{index}",
                        source="api",
                        agent_name="primary",
                        model=model,
                    )
                    for index in range(5)
                )
            )

            self.assertEqual(sorted(run.sequence for run in runs), [1, 2, 3, 4, 5])
            listed = await store.list_runs("session-a")
            self.assertEqual([run.sequence for run in listed], [5, 4, 3, 2, 1])

    async def test_create_run_rejects_unknown_session_without_partial_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()

            with self.assertRaisesRegex(KeyError, "session not found"):
                await store.create_run(
                    session_id="missing",
                    trigger_message_id=None,
                    source="api",
                    agent_name="primary",
                    model=ModelRef(provider="openai-compatible", id="test-model"),
                )

            self.assertEqual(await store.list_runs("missing"), [])

    async def test_deleting_session_deletes_its_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await self._create_session(store, "session-a", tmp)
            run = await store.create_run(
                session_id="session-a",
                trigger_message_id=None,
                source="api",
                agent_name="primary",
                model=ModelRef(provider="openai-compatible", id="test-model"),
            )

            await store.delete_session("session-a")

            with self.assertRaisesRegex(KeyError, "run not found"):
                await store.get_run(run.id)


if __name__ == "__main__":
    unittest.main()

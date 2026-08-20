from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.config import _build_config
from nexapilot.memory import MemoryService
from nexapilot.model import Message, ModelRef, Session, TextPart
from nexapilot.store.sqlite import SQLiteStore


class MemoryPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def _completed_run(
        self,
        store: SQLiteStore,
        worktree: str,
        *,
        user_text: str = "Implement durable memory",
    ):
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
        await store.add_part(
            "session-1",
            user.id,
            TextPart(
                id="user-text",
                message_id=user.id,
                session_id="session-1",
                text=user_text,
            ),
        )
        run = await store.create_run(
            session_id="session-1",
            trigger_message_id=user.id,
            source="api",
            agent_name="primary",
            model=model,
        )
        input_sequence = await store.attach_message_to_run(user.id, run.id)
        await store.start_run(run.id, input_sequence=input_sequence)
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
        await store.add_part(
            "session-1",
            assistant.id,
            TextPart(
                id="assistant-text",
                message_id=assistant.id,
                session_id="session-1",
                text="Durable memory implemented.",
            ),
        )
        _, event = await store.finish_assistant_run_with_outbox(
            assistant_message_id=assistant.id,
            completed_at=3,
            finish_reason="stop",
            run_status="completed",
        )
        return run, event

    @staticmethod
    def _config(
        db_path: str,
        worktree: str,
        *,
        processing: bool,
        episodic: bool = True,
        semantic: bool = False,
    ):
        return _build_config(
            {
                "db_path": db_path,
                "default_worktree": worktree,
                "openai": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                "memory": {
                    "enabled": True,
                    "sync_interval_seconds": 3600,
                    "processing": {"enabled": processing, "worker_interval_ms": 100},
                    "episodic": {"enabled": episodic},
                    "semantic": {"enabled": semantic},
                },
            }
        )

    async def test_completed_run_flows_from_outbox_to_episode_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()
            run, event = await self._completed_run(store, tmp)
            service = MemoryService(
                cfg=self._config(db_path, tmp, processing=True),
                store=store,
                embedding_provider_factory=lambda _cfg: None,
            )

            self.assertEqual(await service.process_pending_once(now_ms=event.created_at), 1)
            episode = await store.get_episode(run.id)
            self.assertEqual(episode.goal, "Implement durable memory")
            checkpoint = await store.get_memory_checkpoint("episodic", "session-1")
            self.assertEqual(checkpoint.last_message_sequence, event.sequence_to)
            self.assertEqual((await store.get_outbox_event(event.id)).status, "processed")
            self.assertEqual(await service.process_pending_once(now_ms=event.created_at + 1), 0)

    async def test_disabled_processing_leaves_outbox_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()
            _, event = await self._completed_run(store, tmp)
            service = MemoryService(
                cfg=self._config(db_path, tmp, processing=False),
                store=store,
                embedding_provider_factory=lambda _cfg: None,
            )

            self.assertEqual(await service.process_pending_once(now_ms=event.created_at), 0)
            self.assertEqual((await store.get_outbox_event(event.id)).status, "pending")

    async def test_semantic_only_pipeline_processes_explicit_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()
            _, event = await self._completed_run(store, tmp, user_text="P3 先不用做")
            service = MemoryService(
                cfg=self._config(
                    db_path,
                    tmp,
                    processing=True,
                    episodic=False,
                    semantic=True,
                ),
                store=store,
                embedding_provider_factory=lambda _cfg: None,
            )

            self.assertEqual(await service.process_pending_once(now_ms=event.created_at), 1)
            memories = await store.list_semantic_memories(str(Path(tmp).resolve()))
            self.assertEqual([(item.subject, item.value) for item in memories], [("P3", "paused")])
            checkpoint = await store.get_memory_checkpoint("semantic", "session-1")
            self.assertEqual(checkpoint.last_message_sequence, event.sequence_to)


if __name__ == "__main__":
    unittest.main()

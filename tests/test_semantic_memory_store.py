from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from nexapilot.model import SemanticMemory
from nexapilot.store.sqlite import SQLiteStore


class SemanticMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _memory(
        *,
        memory_id: str = "memory-1",
        workspace: str = "workspace-a",
        value: str = "P3 is paused",
        status: str = "active",
    ) -> SemanticMemory:
        return SemanticMemory(
            id=memory_id,
            namespace="project",
            workspace=workspace,
            memory_type="constraint",
            subject="P3",
            predicate="status",
            value=value,
            status=status,
            confidence=0.95,
            importance=0.9,
            valid_from=10,
            source_session_id="session-1",
            source_run_id="run-1",
            source_message_ids=["message-1"],
            content_hash=f"hash-{memory_id}",
            version=1,
            extractor_version="semantic-v1",
            created_at=10,
            updated_at=10,
        )

    async def test_put_list_and_search_are_workspace_and_namespace_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            first = self._memory()
            other = self._memory(memory_id="memory-2", workspace="workspace-b")
            await store.put_semantic_memory(first)
            await store.put_semantic_memory(other)

            self.assertEqual(await store.get_semantic_memory(first.id), first)
            self.assertEqual(await store.list_semantic_memories("workspace-a"), [first])
            hits = await store.search_semantic_memories("workspace-a", "P3 paused")
            self.assertEqual([hit.memory.id for hit in hits], [first.id])
            self.assertEqual(await store.search_semantic_memories("workspace-b", "missing"), [])

    async def test_only_one_active_value_exists_for_a_semantic_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await store.put_semantic_memory(self._memory())
            conflicting = self._memory(memory_id="memory-2", value="P3 is active")

            with self.assertRaises(sqlite3.IntegrityError):
                await store.put_semantic_memory(conflicting)
            self.assertEqual(len(await store.list_semantic_memories("workspace-a")), 1)

    async def test_deleted_and_rejected_records_are_not_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            deleted = self._memory(status="deleted")
            await store.put_semantic_memory(deleted)

            self.assertEqual(await store.list_semantic_memories("workspace-a", status="deleted"), [deleted])
            self.assertEqual(await store.search_semantic_memories("workspace-a", "P3 paused"), [])
            await store.delete_semantic_memory(deleted.id)
            with self.assertRaises(KeyError):
                await store.get_semantic_memory(deleted.id)

    async def test_activate_supersedes_conflicting_value_and_noops_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            original = self._memory()
            action, active = await store.activate_semantic_memory(original)
            self.assertEqual(action, "ADD")
            self.assertEqual(active.version, 1)

            duplicate = original.model_copy(update={"id": "memory-duplicate", "updated_at": 20})
            action, unchanged = await store.activate_semantic_memory(duplicate)
            self.assertEqual(action, "NOOP")
            self.assertEqual(unchanged.id, original.id)

            replacement = self._memory(
                memory_id="memory-2",
                value="P3 is active",
            ).model_copy(update={"content_hash": "hash-new", "valid_from": 30, "updated_at": 30})
            action, active = await store.activate_semantic_memory(replacement)
            self.assertEqual(action, "SUPERSEDE")
            self.assertEqual(active.version, 2)
            previous = await store.get_semantic_memory(original.id)
            self.assertEqual(previous.status, "superseded")
            self.assertEqual(previous.valid_to, 30)
            self.assertEqual(await store.list_semantic_memories("workspace-a"), [active])

    async def test_candidate_requires_explicit_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            candidate = self._memory(status="candidate").model_copy(
                update={"source_kind": "subagent", "source_agent": "explore"}
            )
            await store.put_semantic_memory(candidate)

            self.assertEqual(await store.list_semantic_memories("workspace-a"), [])
            self.assertEqual(await store.search_semantic_memories("workspace-a", "P3 paused"), [])
            action, promoted = await store.promote_semantic_memory(candidate.id, now_ms=40)
            self.assertEqual(action, "ADD")
            self.assertEqual(promoted.status, "active")
            self.assertEqual(promoted.valid_from, 40)
            self.assertEqual(await store.promote_semantic_memory(candidate.id, now_ms=50), ("NOOP", promoted))

    async def test_delete_active_is_idempotent_and_removes_search_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await store.activate_semantic_memory(self._memory())
            key = {
                "workspace": "workspace-a",
                "namespace": "project",
                "memory_type": "constraint",
                "subject": "P3",
                "predicate": "status",
            }

            action, deleted = await store.delete_active_semantic_memory(**key, now_ms=50)
            self.assertEqual(action, "DELETE")
            self.assertIsNotNone(deleted)
            assert deleted is not None
            self.assertEqual(deleted.status, "deleted")
            self.assertEqual(deleted.valid_to, 50)
            self.assertEqual(await store.search_semantic_memories("workspace-a", "P3 paused"), [])
            self.assertEqual(await store.delete_active_semantic_memory(**key, now_ms=60), ("NOOP", None))

    async def test_search_supports_chinese_partial_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            memory = self._memory().model_copy(
                update={
                    "memory_type": "preference",
                    "subject": "user",
                    "predicate": "workflow",
                    "value": "每个需求独立提交",
                    "content_hash": "chinese-memory",
                }
            )
            await store.activate_semantic_memory(memory)

            hits = await store.search_semantic_memories("workspace-a", "需求独立")
            self.assertEqual([hit.memory.id for hit in hits], [memory.id])

    async def test_forget_is_soft_idempotent_and_visible_to_governance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            memory = self._memory()
            await store.activate_semantic_memory(memory)

            forgotten = await store.forget_semantic_memory(memory.id, now_ms=50)
            repeated = await store.forget_semantic_memory(memory.id, now_ms=60)
            self.assertEqual(forgotten.status, "deleted")
            self.assertEqual(forgotten.valid_to, 50)
            self.assertEqual(repeated.valid_to, 50)
            self.assertEqual(await store.list_semantic_memories("workspace-a"), [])
            self.assertEqual(
                await store.list_semantic_memories("workspace-a", status="deleted"),
                [forgotten],
            )

            status = await store.get_memory_processing_status()
            self.assertEqual(status, {"outbox": {}, "checkpoints": {}})


if __name__ == "__main__":
    unittest.main()

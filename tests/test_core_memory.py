from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from nexapilot.memory import CoreMemoryBuilder
from nexapilot.model import SemanticMemory
from nexapilot.store.sqlite import SQLiteStore


def _memory(memory_id: str, memory_type: str, subject: str, predicate: str, value: str, importance: float):
    content_hash = hashlib.sha256(f"{memory_type}\n{subject}\n{predicate}\n{value}".encode()).hexdigest()
    return SemanticMemory(
        id=memory_id,
        namespace="project",
        workspace="workspace-a",
        memory_type=memory_type,
        subject=subject,
        predicate=predicate,
        value=value,
        status="active",
        confidence=0.95,
        importance=importance,
        source_session_id="session-1",
        source_message_ids=["message-1"],
        content_hash=content_hash,
        extractor_version="test",
        created_at=1,
        updated_at=1,
    )


class CoreMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_governed_blocks_under_budget_and_versions_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            memories = [
                _memory("constraint-1", "constraint", "P3", "status", "paused", 1.0),
                _memory("preference-1", "preference", "user", "workflow", "one commit per requirement", 0.9),
                _memory("decision-1", "decision", "project", "name", "Nexa", 0.8),
            ]
            for memory in memories:
                await store.activate_semantic_memory(memory)
            builder = CoreMemoryBuilder(store=store, max_tokens=100)

            first = await builder.rebuild("workspace-a", now_ms=10)
            rendered = builder.render(first)
            self.assertIn("<active_constraints>", rendered)
            self.assertIn("P3 status: paused", rendered)
            self.assertIn("one commit per requirement", rendered)
            self.assertLessEqual(sum(block.token_count for block in first), 100)
            self.assertTrue(all(block.version == 1 for block in first))

            unchanged = await builder.rebuild("workspace-a", now_ms=20)
            self.assertTrue(all(block.version == 1 for block in unchanged))

            replacement = _memory("constraint-2", "constraint", "P3", "status", "active", 1.0)
            await store.activate_semantic_memory(replacement)
            changed = await builder.rebuild("workspace-a", now_ms=30)
            constraint = next(block for block in changed if block.block_type == "active_constraints")
            self.assertEqual(constraint.version, 2)
            self.assertIn("P3 status: active", constraint.content)

    async def test_empty_projection_removes_stale_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            memory = _memory("constraint-1", "constraint", "P3", "status", "paused", 1.0)
            await store.activate_semantic_memory(memory)
            builder = CoreMemoryBuilder(store=store)
            await builder.rebuild("workspace-a", now_ms=10)
            await store.delete_active_semantic_memory(
                workspace="workspace-a",
                namespace="project",
                memory_type="constraint",
                subject="P3",
                predicate="status",
                now_ms=20,
            )

            self.assertEqual(await builder.rebuild("workspace-a", now_ms=20), [])
            self.assertEqual(await store.list_core_memory_blocks("workspace-a"), [])


if __name__ == "__main__":
    unittest.main()

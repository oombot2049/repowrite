from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.model import Episode
from nexapilot.store.sqlite import SQLiteStore


class EpisodicStoreTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _episode(
        *,
        episode_id: str = "episode-1",
        workspace: str = "workspace-a",
        updated_at: int = 10,
    ) -> Episode:
        return Episode(
            id=episode_id,
            workspace=workspace,
            source_session_id=f"session-{episode_id}",
            source_run_id=f"run-{episode_id}",
            sequence_from=1,
            sequence_to=4,
            goal="Fix Responses API tool calling",
            actions=["Inspect provider errors", "Implement encrypted reasoning replay"],
            outcome="success",
            errors=["Chat Completions rejected reasoning tools"],
            artifacts=["src/nexapilot/llm/openai_responses.py"],
            lessons=["Replay provider state when store is disabled"],
            extractor_version="episodic-v1",
            created_at=1,
            updated_at=updated_at,
        )

    async def test_upsert_lists_and_searches_with_workspace_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            first = self._episode()
            other = self._episode(episode_id="episode-2", workspace="workspace-b")
            await store.upsert_episode(first)
            await store.upsert_episode(other)

            self.assertEqual(await store.get_episode(first.id), first)
            self.assertEqual(await store.list_episodes("workspace-a"), [first])
            hits = await store.search_episodes("workspace-a", "Responses tool", limit=5)
            self.assertEqual([hit.episode.id for hit in hits], [first.id])
            self.assertGreater(hits[0].score, 0)
            other_hits = await store.search_episodes("workspace-b", "encrypted replay")
            self.assertEqual([hit.episode.id for hit in other_hits], [other.id])
            self.assertEqual(await store.search_episodes("workspace-a", "missing phrase"), [])

    async def test_upsert_replaces_fts_projection_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            original = self._episode()
            changed = original.model_copy(
                update={
                    "goal": "Repair streaming event conversion",
                    "actions": ["Normalize response events"],
                    "updated_at": 20,
                }
            )
            await store.upsert_episode(original)
            await store.upsert_episode(changed)

            self.assertEqual(await store.get_episode(original.id), changed)
            self.assertEqual(await store.search_episodes("workspace-a", "Fix"), [])
            hits = await store.search_episodes("workspace-a", "streaming conversion")
            self.assertEqual([hit.episode.id for hit in hits], [original.id])

    async def test_delete_removes_record_and_search_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            episode = self._episode()
            await store.upsert_episode(episode)
            await store.delete_episode(episode.id)

            with self.assertRaises(KeyError):
                await store.get_episode(episode.id)
            self.assertEqual(await store.search_episodes("workspace-a", "Responses"), [])

    async def test_search_supports_chinese_partial_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            episode = self._episode().model_copy(
                update={"goal": "修复响应接口工具调用", "actions": ["补充事件转换"]}
            )
            await store.upsert_episode(episode)

            hits = await store.search_episodes("workspace-a", "响应接口")
            self.assertEqual([hit.episode.id for hit in hits], [episode.id])


if __name__ == "__main__":
    unittest.main()

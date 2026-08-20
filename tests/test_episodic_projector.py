from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.memory import EpisodicProjector
from nexapilot.model import (
    Message,
    ModelRef,
    OutboxEvent,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
)
from nexapilot.store.sqlite import SQLiteStore


class EpisodicProjectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_projects_run_without_copying_full_tool_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await store.create_session(
                Session(
                    id="session-1",
                    title="fallback title",
                    worktree=tmp,
                    cwd=tmp,
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
                    text="Fix Responses API using token=secret-value",
                ),
            )
            run = await store.create_run(
                session_id="session-1",
                trigger_message_id=user.id,
                source="api",
                agent_name="primary",
                model=model,
                now_ms=2,
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
                    created_at=3,
                )
            )
            await store.add_part(
                "session-1",
                assistant.id,
                ToolPart(
                    id="tool-1",
                    message_id=assistant.id,
                    session_id="session-1",
                    call_id="call-1",
                    tool="write",
                    state=ToolStateCompleted(
                        input={"file_path": "src/provider.py", "content": "do not persist this body"},
                        title="src/provider.py",
                        output="very large output that must remain only in Session Store",
                        metadata={"file_path": "src/provider.py"},
                        time={"start": 3, "end": 4},
                    ),
                ),
            )
            await store.add_part(
                "session-1",
                assistant.id,
                TextPart(
                    id="assistant-text",
                    message_id=assistant.id,
                    session_id="session-1",
                    text="Implemented Responses provider replay.",
                ),
            )
            completed = await store.finish_run(
                run.id,
                status="completed",
                assistant_message_id=assistant.id,
                finish_reason="stop",
            )
            messages = await store.list_messages("session-1")
            event = OutboxEvent(
                id="event-1",
                idempotency_key=f"run.completed:{run.id}",
                event_type="run.completed",
                aggregate_type="run",
                aggregate_id=run.id,
                session_id="session-1",
                run_id=run.id,
                sequence_from=completed.input_sequence,
                sequence_to=completed.output_sequence,
                payload={"status": "completed"},
                created_at=5,
            )

            await EpisodicProjector(store=store)(event, messages)
            episode = await store.get_episode(run.id)

            self.assertEqual(episode.goal, "Fix Responses API using token=[REDACTED]")
            self.assertEqual(episode.outcome, "completed")
            self.assertEqual(episode.actions, ["write: src/provider.py (completed)"])
            self.assertEqual(episode.artifacts, ["src/provider.py"])
            self.assertEqual(episode.lessons, ["Implemented Responses provider replay."])
            self.assertNotIn("very large output", episode.model_dump_json())
            self.assertEqual(episode.sequence_from, 1)
            self.assertEqual(episode.sequence_to, 2)
            self.assertEqual((episode.source_kind, episode.source_agent), ("primary", "primary"))

            await EpisodicProjector(store=store)(event, messages)
            self.assertEqual(len(await store.list_episodes(str(Path(tmp).resolve()))), 1)

            child_episode = episode.model_copy(
                update={
                    "id": "run-child",
                    "source_run_id": "run-child",
                    "source_session_id": "session-child",
                    "source_kind": "subagent",
                    "source_agent": "explore",
                    "updated_at": episode.updated_at + 1,
                }
            )
            await store.upsert_episode(child_episode)
            hits = await store.search_episodes(
                str(Path(tmp).resolve()),
                "Responses provider",
                limit=2,
                subagent_weight=0.6,
            )
            self.assertEqual([hit.episode.id for hit in hits], [run.id, "run-child"])
            self.assertGreater(hits[0].score, hits[1].score)


if __name__ == "__main__":
    unittest.main()

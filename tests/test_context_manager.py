from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from nexapilot.config import MemoryContextManagerConfig
from nexapilot.memory import ContextManager, CoreMemoryBuilder
from nexapilot.model import (
    Message,
    MessageWithParts,
    ModelRef,
    SemanticMemory,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
)
from nexapilot.store.sqlite import SQLiteStore


def _message(message_id: str, role: str, sequence: int, text: str, *, run_id: str | None):
    info = Message(
        id=message_id,
        session_id="session-1",
        run_id=run_id,
        sequence=sequence,
        role=role,
        agent="primary",
        model=ModelRef(provider="openai-compatible", id="test"),
        created_at=sequence,
        tool_call_id=f"call-{run_id}" if role == "tool" else None,
        tool_name="bash" if role == "tool" else None,
    )
    return MessageWithParts(
        info=info,
        parts=[TextPart(id=f"part-{message_id}", message_id=message_id, session_id="session-1", text=text)],
    )


class ContextManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_history_budget_keeps_each_run_atomic(self) -> None:
        history = [
            _message("user-1", "user", 1, "old request", run_id="run-1"),
            _message("assistant-1", "assistant", 2, "x" * 500, run_id="run-1"),
            _message("tool-1", "tool", 3, "x" * 500, run_id="run-1"),
            _message("user-2", "user", 4, "new request", run_id="run-2"),
            _message("assistant-2", "assistant", 5, "done", run_id="run-2"),
            _message("tool-2", "tool", 6, "tool output", run_id="run-2"),
            _message("current-user", "user", 7, "current question", run_id=None),
        ]

        selected = ContextManager._select_recent_units(history, budget_tokens=160)
        selected_ids = [message.info.id for message in selected]
        self.assertIn("current-user", selected_ids)
        self.assertEqual(
            {message.info.id for message in history if message.info.run_id == "run-2"}.issubset(selected_ids),
            True,
        )
        run_one_selected = [message.info.id for message in selected if message.info.run_id == "run-1"]
        self.assertIn(len(run_one_selected), {0, 3})

    async def test_build_combines_core_memory_and_recent_history_with_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            session = Session(
                id="session-1",
                title="test",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[],
            )
            await store.create_session(session)
            value = "one commit per requirement"
            content_hash = hashlib.sha256(f"preference\nuser\nworkflow\n{value}".encode()).hexdigest()
            await store.activate_semantic_memory(
                SemanticMemory(
                    id="memory-1",
                    namespace="project",
                    workspace=str(Path(tmp).resolve()),
                    memory_type="preference",
                    subject="user",
                    predicate="workflow",
                    value=value,
                    status="active",
                    confidence=0.95,
                    importance=0.9,
                    source_session_id="session-1",
                    source_message_ids=["source-message"],
                    content_hash=content_hash,
                    extractor_version="test",
                    created_at=1,
                    updated_at=1,
                )
            )
            await CoreMemoryBuilder(store=store).rebuild(str(Path(tmp).resolve()), now_ms=2)
            history = [_message("current-user", "user", 1, "How should we commit?", run_id=None)]
            manager = ContextManager(
                store=store,
                config=MemoryContextManagerConfig(
                    enabled=True,
                    shadow_mode=False,
                    max_input_tokens=2_000,
                    reserved_output_tokens=500,
                ),
            )

            result = await manager.build(session=session, history=history, system_text="system")
            self.assertEqual([message.info.id for message in result.history], ["current-user"])
            self.assertIn("<core_memory>", result.memory_context)
            self.assertIn(value, result.memory_context)
            self.assertEqual(result.stats["history_messages_selected"], 1)
            self.assertEqual(result.stats["core_blocks"], 1)
            self.assertEqual(result.stats["budget_overflow_tokens"], 0)

    async def test_build_truncates_latest_atomic_unit_to_hard_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            session = Session(
                id="session-1",
                title="test",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[],
            )
            await store.create_session(session)
            history = [
                _message("user-1", "user", 1, "request", run_id="run-1"),
                _message("tool-1", "tool", 2, "x" * 1_000, run_id="run-1"),
            ]
            manager = ContextManager(
                store=store,
                config=MemoryContextManagerConfig(
                    enabled=True,
                    shadow_mode=True,
                    max_input_tokens=200,
                    reserved_output_tokens=100,
                ),
            )

            result = await manager.build(session=session, history=history, system_text="system")

            self.assertEqual(len(result.history), 2)
            self.assertEqual(result.stats["budget_overflow_tokens"], 0)
            tool_text = next(
                part.text
                for message in result.history
                if message.info.role == "tool"
                for part in message.parts
                if isinstance(part, TextPart)
            )
            self.assertIn("truncated by Context Manager", tool_text)

    def test_long_run_keeps_user_and_newest_complete_tool_cycles(self) -> None:
        model = ModelRef(provider="openai-compatible", id="test")
        user = _message(
            "current-user", "user", 1, "inspect this repository", run_id="run-1"
        )
        assistant = MessageWithParts(
            info=Message(
                id="assistant",
                session_id="session-1",
                run_id="run-1",
                sequence=2,
                role="assistant",
                agent="primary",
                model=model,
                created_at=2,
            ),
            parts=[
                ToolPart(
                    id=f"part-{index}",
                    message_id="assistant",
                    session_id="session-1",
                    call_id=f"call-{index}",
                    tool="read",
                    state=ToolStateCompleted(
                        input={"file_path": f"file-{index}.py"},
                        title=f"file-{index}.py",
                        output="x" * 2_000,
                        metadata={},
                        time={"start": index, "end": index + 1},
                    ),
                )
                for index in range(40)
            ],
        )
        tool_messages = [
            _message(
                f"tool-{index}",
                "tool",
                index + 3,
                "result-" + ("x" * 2_000),
                run_id="run-1",
            )
            for index in range(40)
        ]
        for index, message in enumerate(tool_messages):
            message.info.tool_call_id = f"call-{index}"

        selected = ContextManager._select_recent_units(
            [user, assistant, *tool_messages],
            budget_tokens=1_000,
        )

        self.assertTrue(selected)
        self.assertEqual(selected[0].info.id, "current-user")
        selected_tool_ids = [
            message.info.tool_call_id
            for message in selected
            if message.info.role == "tool"
        ]
        self.assertTrue(selected_tool_ids)
        self.assertEqual(selected_tool_ids[-1], "call-39")
        kept_calls = {
            part.call_id
            for message in selected
            for part in message.parts
            if isinstance(part, ToolPart)
        }
        self.assertEqual(kept_calls, set(selected_tool_ids))
        self.assertLessEqual(
            sum(ContextManager._message_tokens(message) for message in selected),
            1_000,
        )


if __name__ == "__main__":
    unittest.main()

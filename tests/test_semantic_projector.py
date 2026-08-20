from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.memory import RuleBasedSemanticExtractor, SemanticProjector
from nexapilot.model import Message, MessageWithParts, ModelRef, OutboxEvent, Session, TextPart
from nexapilot.store.sqlite import SQLiteStore


def _message(message_id: str, run_id: str, sequence: int, text: str) -> MessageWithParts:
    info = Message(
        id=message_id,
        session_id="session-1",
        run_id=run_id,
        sequence=sequence,
        role="user",
        agent="primary",
        model=ModelRef(provider="openai-compatible", id="test"),
        created_at=sequence,
    )
    return MessageWithParts(
        info=info,
        parts=[TextPart(id=f"part-{message_id}", message_id=message_id, session_id="session-1", text=text)],
    )


def _event(run_id: str, sequence: int) -> OutboxEvent:
    return OutboxEvent(
        id=f"event-{run_id}",
        idempotency_key=f"run.completed:{run_id}",
        event_type="run.completed",
        aggregate_type="run",
        aggregate_id=run_id,
        session_id="session-1",
        run_id=run_id,
        sequence_from=sequence,
        sequence_to=sequence,
        payload={},
        created_at=sequence * 10,
    )


class SemanticProjectorTests(unittest.IsolatedAsyncioTestCase):
    def test_extractor_is_conservative_and_drops_secret_bearing_sentence(self) -> None:
        message = _message(
            "message-1",
            "run-1",
            1,
            "P3 先不用做。以后请每个需求独立 commit。请记住 API_KEY=sk-abcdefghijklmnop",
        )
        candidates = RuleBasedSemanticExtractor().extract([message])

        self.assertEqual([(item.subject, item.predicate, item.value) for item in candidates], [
            ("P3", "status", "paused"),
            ("user", "workflow", "以后请每个需求独立 commit"),
        ])

    def test_remembered_fact_strips_chinese_and_ascii_colons(self) -> None:
        messages = [
            _message("message-1", "run-1", 1, "请记住：项目使用 Responses API"),
            _message("message-2", "run-1", 2, "记住: 数据库使用 SQLite"),
        ]

        candidates = RuleBasedSemanticExtractor().extract(messages)

        self.assertEqual(
            [item.value for item in candidates],
            ["项目使用 Responses API", "数据库使用 SQLite"],
        )

    def test_remembered_content_is_classified_by_meaning(self) -> None:
        messages = [
            _message(
                "message-1",
                "run-1",
                1,
                "请记住：我准备 Agent 面试时，优先复习 Agent Loop、Tool Calling、Memory 和权限审批。",
            ),
            _message("message-2", "run-1", 2, "请记住：项目必须保持 Workspace 隔离。"),
            _message("message-3", "run-1", 3, "请记住：我的目标是完成生产验收。"),
        ]

        candidates = RuleBasedSemanticExtractor().extract(messages)

        self.assertEqual(
            [(item.memory_type, item.subject, item.predicate, item.value) for item in candidates],
            [
                (
                    "preference",
                    "user",
                    "workflow",
                    "我准备 Agent 面试时，优先复习 Agent Loop、Tool Calling、Memory 和权限审批",
                ),
                ("constraint", "project", "operating_rule", "项目必须保持 Workspace 隔离"),
                ("goal", "user", "active_goal", "我的目标是完成生产验收"),
            ],
        )

    async def test_new_explicit_decision_supersedes_previous_active_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await store.create_session(
                Session(
                    id="session-1",
                    title="test",
                    worktree=tmp,
                    cwd=tmp,
                    created_at=1,
                    updated_at=1,
                    permission_rules=[],
                )
            )
            projector = SemanticProjector(store=store)
            await projector(_event("run-1", 1), [_message("message-1", "run-1", 1, "P3 先不用做")])
            await projector(_event("run-2", 2), [_message("message-2", "run-2", 2, "现在开始 P3")])

            workspace = str(Path(tmp).resolve())
            active = await store.list_semantic_memories(workspace)
            all_records = await store.list_semantic_memories(workspace, status=None)
            self.assertEqual([(item.subject, item.predicate, item.value) for item in active], [
                ("P3", "status", "active")
            ])
            self.assertEqual({item.status for item in all_records}, {"active", "superseded"})
            self.assertEqual(active[0].version, 2)
            self.assertEqual(active[0].source_message_ids, ["message-2"])

    async def test_subagent_prompt_is_not_activated_and_conclusion_is_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            await store.create_session(
                Session(
                    id="session-1",
                    title="explore",
                    worktree=tmp,
                    cwd=tmp,
                    kind="subagent",
                    agent_name="explore",
                    root_session_id="root-1",
                    parent_session_id="root-1",
                    parent_tool_call_id="call-1",
                    created_at=1,
                    updated_at=1,
                    permission_rules=[],
                )
            )
            user = _message("child-user", "run-child", 1, "必须不要修改文件")
            assistant = MessageWithParts(
                info=Message(
                    id="child-assistant",
                    session_id="session-1",
                    run_id="run-child",
                    sequence=2,
                    role="assistant",
                    agent="explore",
                    model=ModelRef(provider="openai-compatible", id="test"),
                    created_at=2,
                ),
                parts=[
                    TextPart(
                        id="part-child-assistant",
                        message_id="child-assistant",
                        session_id="session-1",
                        text="发现配置入口位于 src/config.py。",
                    )
                ],
            )

            await SemanticProjector(store=store)(
                _event("run-child", 2),
                [user, assistant],
            )

            workspace = str(Path(tmp).resolve())
            self.assertEqual(await store.list_semantic_memories(workspace), [])
            candidates = await store.list_semantic_memories(workspace, status="candidate")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate.value, "发现配置入口位于 src/config.py。")
            self.assertEqual((candidate.source_kind, candidate.source_agent), ("subagent", "explore"))
            self.assertNotIn("不要修改", candidate.value)
            self.assertEqual(await store.search_semantic_memories(workspace, "配置入口"), [])

            action, active = await store.promote_semantic_memory(candidate.id, now_ms=30)
            self.assertEqual(action, "ADD")
            self.assertEqual(active.status, "active")
            self.assertEqual(
                [hit.memory.id for hit in await store.search_semantic_memories(workspace, "配置入口")],
                [candidate.id],
            )

            await SemanticProjector(store=store)(
                _event("run-child", 2),
                [user, assistant],
            )
            self.assertEqual((await store.get_semantic_memory(candidate.id)).status, "active")


if __name__ == "__main__":
    unittest.main()

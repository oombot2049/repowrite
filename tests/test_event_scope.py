from __future__ import annotations

import tempfile
import unittest
import importlib
from pathlib import Path

from nexapilot.bus.bus import Event
from nexapilot.model import Session
from nexapilot.store.sqlite import SQLiteStore


api_app = importlib.import_module("nexapilot.api.app")


class EventScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tree_scope_includes_child_context_and_blocks_other_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "events.sqlite3"))
            await store.init()
            root = Session(
                id="root",
                title="root",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[],
                root_session_id="root",
            )
            child = Session(
                id="child",
                title="child",
                worktree=tmp,
                cwd=tmp,
                created_at=2,
                updated_at=2,
                permission_rules=[],
                kind="subagent",
                agent_name="explore",
                root_session_id="root",
                parent_session_id="root",
                parent_tool_call_id="task-call",
            )
            other = Session(
                id="other",
                title="other",
                worktree=tmp,
                cwd=tmp,
                created_at=3,
                updated_at=3,
                permission_rules=[],
                root_session_id="other",
            )
            for session in (root, child, other):
                await store.create_session(session)

            original_store = api_app.store
            api_app.store = store
            try:
                caches: tuple[dict[str, str | None], dict[str, Session | None]] = (
                    {},
                    {},
                )
                child_props = await api_app._scope_event_properties(
                    Event(
                        type="message.updated",
                        properties={"session_id": "child"},
                    ),
                    target_session_id="root",
                    target_root_id="root",
                    scope="tree",
                    run_sessions=caches[0],
                    session_contexts=caches[1],
                )
                exact_props = await api_app._scope_event_properties(
                    Event(type="session.status", properties={"session_id": "child"}),
                    target_session_id="root",
                    target_root_id="root",
                    scope="session",
                    run_sessions=caches[0],
                    session_contexts=caches[1],
                )
                other_props = await api_app._scope_event_properties(
                    Event(type="session.status", properties={"session_id": "other"}),
                    target_session_id="root",
                    target_root_id="root",
                    scope="tree",
                    run_sessions=caches[0],
                    session_contexts=caches[1],
                )
                unscoped_props = await api_app._scope_event_properties(
                    Event(type="provider.notice", properties={"detail": "global"}),
                    target_session_id="root",
                    target_root_id="root",
                    scope="tree",
                    run_sessions=caches[0],
                    session_contexts=caches[1],
                )
            finally:
                api_app.store = original_store

            self.assertIsNotNone(child_props)
            assert child_props is not None
            self.assertEqual(child_props["root_session_id"], "root")
            self.assertEqual(child_props["parent_session_id"], "root")
            self.assertEqual(child_props["parent_tool_call_id"], "task-call")
            self.assertEqual(child_props["agent"], "explore")
            self.assertEqual(child_props["session_kind"], "subagent")
            self.assertIsNone(exact_props)
            self.assertIsNone(other_props)
            self.assertIsNone(unscoped_props)


if __name__ == "__main__":
    unittest.main()

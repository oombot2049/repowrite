from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from unittest.mock import patch

from nexapilot.cli.commands.run import _quick_run


class FakeClient:
    instance: "FakeClient | None" = None

    def __init__(self, _base_url: str) -> None:
        self.reply_received = asyncio.Event()
        self.posts: list[tuple[str, dict | None]] = []
        FakeClient.instance = self

    async def ping(self) -> bool:
        return True

    async def post(self, path: str, json: dict | None = None):
        self.posts.append((path, json))
        if path == "/sessions":
            return {"id": "session-1"}
        if path == "/sessions/session-1/run":
            await asyncio.wait_for(self.reply_received.wait(), timeout=2)
            return {"assistant_message_id": "assistant-1"}
        if path == "/permissions/request-1/reply":
            self.reply_received.set()
            return {"ok": True}
        return {"ok": True}

    async def stream_sse(self, _path: str, *, params: dict):
        assert params == {"session_id": "session-1"}
        yield {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "running", "title": None},
                }
            },
        }
        yield {
            "type": "permission.asked",
            "properties": {"id": "request-1", "permission": "bash"},
        }
        await asyncio.wait_for(self.reply_received.wait(), timeout=2)
        yield {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "title": "bash"},
                }
            },
        }
        yield {
            "type": "message.part.updated",
            "properties": {"part": {"type": "text"}, "delta": "done"},
        }
        yield {"type": "session.loop.done", "properties": {}}


class QuickRunEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_asked_is_replied_and_tool_events_are_rendered(self) -> None:
        output = io.StringIO()
        with patch("nexapilot.cli.commands.run.Client", FakeClient), contextlib.redirect_stdout(output):
            await _quick_run(
                base_url="http://local",
                message="run",
                model=None,
                permission="allow",
                worktree="/workspace",
                runtime="local",
                daytona_sandbox_id="",
                session_id=None,
                no_stream=False,
                cleanup=False,
                json_mode=False,
            )

        client = FakeClient.instance
        assert client is not None
        self.assertIn(
            ("/permissions/request-1/reply", {"reply": "once"}), client.posts
        )
        rendered = output.getvalue()
        self.assertIn("[tool: bash]", rendered)
        self.assertIn("[permission auto-once: bash]", rendered)
        self.assertIn("[tool done: bash]", rendered)
        self.assertIn("done", rendered)

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nexapilot.bus.bus import Bus
from nexapilot.config import (
    ChannelsConfig,
    Config,
    FeishuChannelConfig,
    HooksConfig,
    KBConfig,
    LangfuseConfig,
    LoggingConfig,
    OpenAIConfig,
    VLMConfig,
    WebSearchConfig,
)
from nexapilot.llm.openai_chat import Finish, ToolCall
from nexapilot.loop.interrupt import InterruptManager
from nexapilot.loop.session_loop import SessionLoop
from nexapilot.model import Message, ModelRef, PermissionRule, Session, TextPart
from nexapilot.permission.service import PermissionService
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.tools.base import ToolResult
from nexapilot.tools.registry import ToolRegistry


class TwoRoundLLM:
    def __init__(self, args: object) -> None:
        self.args = args
        self.calls = 0
        self.inputs: list[list[dict]] = []

    async def stream(self, *, messages: list[dict], **_kwargs):
        self.calls += 1
        self.inputs.append(messages)
        if self.calls == 1:
            yield ToolCall(
                type="tool_call",
                call_id="call-1",
                name="protected",
                args_json=json.dumps(self.args),
            )
            yield Finish(type="finish", reason="tool_calls")
            return
        yield Finish(type="finish", reason="stop")


class ProtectedTool:
    name = "protected"
    description = "A test tool that requires explicit permission."

    def __init__(self) -> None:
        self.executions = 0

    def schema(self):
        return {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }

    async def execute(self, args, ctx):
        await ctx.ask(
            permission="protected",
            patterns=[str(args["value"])],
            always=[str(args["value"])],
            metadata={},
        )
        self.executions += 1
        return ToolResult(title="protected", output="ok", metadata={})


def make_config(db: str, worktree: str) -> Config:
    return Config(
        openai=OpenAIConfig(base_url="http://local", api_key="k", model="m"),
        langfuse=LangfuseConfig(
            enabled=False,
            public_key="",
            secret_key="",
            base_url="http://local",
            environment="test",
            sample_rate=1.0,
            debug=False,
        ),
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=False,
                app_id="",
                app_secret="",
                encrypt_key="",
                verification_token="",
                allow_from=[],
            )
        ),
        kb=KBConfig(backend="none", base_url="", api_key=""),
        vlm=VLMConfig(
            backend="none", api_url="", api_key="", poll_interval=5, timeout=1800
        ),
        logging=LoggingConfig(
            level="INFO",
            console=False,
            file=False,
            dir="./data/logs",
            rotation="00:00",
            retention="7 days",
        ),
        hooks=HooksConfig(debug=False),
        web_search=WebSearchConfig(tavily_api_key=""),
        system_prompt="sys",
        db_path=db,
        default_worktree=worktree,
        default_permission_action="ask",
        prompt_templates={},
    )


class ToolFailureContractTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, args: object, action: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = str(Path(temp.name) / "test.sqlite3")
        store = SQLiteStore(db_path)
        await store.init()
        session = Session(
            id="session-1",
            title="test",
            worktree=temp.name,
            cwd=temp.name,
            created_at=1,
            updated_at=1,
            permission_rules=[
                PermissionRule(permission="*", pattern="*", action=action)
            ],
        )
        await store.create_session(session)
        user = Message(
            id="user-1",
            session_id=session.id,
            role="user",
            parent_id=None,
            agent="primary",
            model=ModelRef(provider="openai-compatible", id="m"),
            created_at=1,
        )
        await store.add_message(user)
        await store.add_part(
            session.id,
            user.id,
            TextPart(
                id="part-1", message_id=user.id, session_id=session.id, text="run"
            ),
        )
        llm = TwoRoundLLM(args)
        tool = ProtectedTool()
        bus = Bus()
        loop = SessionLoop(
            cfg=make_config(db_path, temp.name),
            bus=bus,
            store=store,
            perm=PermissionService(bus, store),
            tools=ToolRegistry([tool]),
            llm=llm,
            interrupt=InterruptManager(),
        )
        _assistant_id, _trace_id = await loop.run(session_id=session.id)
        return store, llm, tool

    async def test_invalid_arguments_are_not_executed_and_return_to_model(self) -> None:
        store, llm, tool = await self._run({"value": "wrong"}, "allow")
        self.assertEqual(tool.executions, 0)
        self.assertEqual(llm.calls, 2)
        self.assertTrue(
            any(
                message.get("role") == "tool"
                and "Invalid tool arguments" in message.get("content", "")
                for message in llm.inputs[1]
            )
        )
        operations = await store.list_tool_operations("session-1")
        self.assertEqual(operations[0]["status"], "error")
        self.assertEqual(operations[0]["error_code"], "invalid_tool_arguments")

    async def test_permission_denial_is_a_tool_result_and_loop_continues(self) -> None:
        store, llm, tool = await self._run({"value": 7}, "deny")
        self.assertEqual(tool.executions, 0)
        self.assertEqual(llm.calls, 2)
        self.assertTrue(
            any(
                message.get("role") == "tool"
                and "Permission denied" in message.get("content", "")
                for message in llm.inputs[1]
            )
        )
        operations = await store.list_tool_operations("session-1")
        self.assertEqual(operations[0]["status"], "error")
        self.assertEqual(operations[0]["error_code"], "permission_denied")
        metadata = operations[0]["result"]["metadata"]
        self.assertEqual(metadata["tool_contract"]["side_effect"], "external_write")
        self.assertEqual(metadata["tool_contract"]["idempotency"], "unsafe")
        self.assertEqual(metadata["recovery_action"], "manual_review")

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

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
from nexapilot.hooks import Hooker
from nexapilot.llm.openai_chat import Finish, ProviderState, ReasoningDelta, ToolCall, _extract_reasoning_text
from nexapilot.loop.interrupt import InterruptManager
from nexapilot.loop.session_loop import SessionLoop
from nexapilot.model import (
    Message,
    MessageWithParts,
    ModelRef,
    PermissionRule,
    ProviderStatePart,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
    ToolStatePending,
)
from nexapilot.permission.service import PermissionService
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.tools.base import ToolResult
from nexapilot.tools.registry import ToolRegistry


class DeltaWithExtra:
    def __init__(self, payload):
        self.model_extra = payload


class ReasoningExtractTests(unittest.TestCase):
    def test_extract_reasoning_from_model_extra(self) -> None:
        delta = DeltaWithExtra(
            {"reasoning_content": [{"text": "first "}, {"content": [{"text": "second"}]}]}
        )
        self.assertEqual(_extract_reasoning_text(delta), "first second")

    def test_provider_state_is_replayed_only_within_its_run(self) -> None:
        def assistant(message_id: str, run_id: str, state_id: str):
            return MessageWithParts(
                info=Message(
                    id=message_id,
                    session_id="session",
                    run_id=run_id,
                    role="assistant",
                    agent="primary",
                    model=ModelRef(provider="openai-compatible", id="model"),
                    created_at=1,
                ),
                parts=[
                    ProviderStatePart(
                        id=f"part-{message_id}",
                        message_id=message_id,
                        session_id="session",
                        provider="openai_responses",
                        data={
                            "type": "reasoning",
                            "id": state_id,
                            "encrypted_content": "opaque",
                        },
                    )
                ],
            )

        loop = object.__new__(SessionLoop)
        messages = loop._to_openai_messages(
            [assistant("old", "run-old", "rs-old"), assistant("new", "run-new", "rs-new")],
            provider_state_run_id="run-new",
        )

        self.assertNotIn("provider_state", messages[0])
        self.assertEqual(messages[1]["provider_state"][0]["id"], "rs-new")

    def test_provider_state_replay_preserves_model_round_order(self) -> None:
        model = ModelRef(provider="openai-compatible", id="model")
        assistant = MessageWithParts(
            info=Message(
                id="assistant",
                session_id="session",
                run_id="run-new",
                role="assistant",
                agent="primary",
                model=model,
                created_at=2,
            ),
            parts=[
                ProviderStatePart(
                    id="state-1",
                    message_id="assistant",
                    session_id="session",
                    provider="openai_responses",
                    data={"type": "reasoning", "id": "rs-1", "encrypted_content": "one"},
                ),
                ToolPart(
                    id="tool-1",
                    message_id="assistant",
                    session_id="session",
                    call_id="call-1",
                    tool="read",
                    state=ToolStatePending(input={"path": "one"}, raw='{"path":"one"}'),
                ),
                ToolPart(
                    id="tool-1",
                    message_id="assistant",
                    session_id="session",
                    call_id="call-1",
                    tool="read",
                    state=ToolStateCompleted(
                        input={"path": "one"},
                        title="one",
                        output="first",
                        metadata={},
                        time={"start": 3, "end": 4},
                    ),
                ),
                ProviderStatePart(
                    id="state-2",
                    message_id="assistant",
                    session_id="session",
                    provider="openai_responses",
                    data={"type": "reasoning", "id": "rs-2", "encrypted_content": "two"},
                ),
                ToolPart(
                    id="tool-2",
                    message_id="assistant",
                    session_id="session",
                    call_id="call-2",
                    tool="read",
                    state=ToolStatePending(input={"path": "two"}, raw='{"path":"two"}'),
                ),
                ProviderStatePart(
                    id="state-3",
                    message_id="assistant",
                    session_id="session",
                    provider="openai_responses",
                    data={"type": "reasoning", "id": "rs-3", "encrypted_content": "three"},
                ),
                TextPart(
                    id="answer",
                    message_id="assistant",
                    session_id="session",
                    text="done",
                ),
            ],
        )

        def tool_message(sequence: int, call_id: str, text: str) -> MessageWithParts:
            message_id = f"result-{call_id}"
            return MessageWithParts(
                info=Message(
                    id=message_id,
                    session_id="session",
                    run_id="run-new",
                    sequence=sequence,
                    role="tool",
                    agent="primary",
                    model=model,
                    created_at=sequence,
                    tool_call_id=call_id,
                    tool_name="read",
                ),
                parts=[
                    TextPart(
                        id=f"text-{call_id}",
                        message_id=message_id,
                        session_id="session",
                        text=text,
                    )
                ],
            )

        loop = object.__new__(SessionLoop)
        messages = loop._to_openai_messages(
            [
                assistant,
                tool_message(3, "call-1", "first"),
                tool_message(4, "call-2", "second"),
            ],
            provider_state_run_id="run-new",
        )

        self.assertEqual(
            [
                message.get("provider_state", [{}])[0].get("id")
                if message.get("role") == "assistant"
                else message.get("tool_call_id")
                for message in messages
            ],
            ["rs-1", "call-1", "rs-2", "call-2", "rs-3"],
        )
        self.assertEqual(messages[-1]["content"], "done")

    def test_quarantined_provider_state_is_not_projected(self) -> None:
        message = MessageWithParts(
            info=Message(
                id="assistant",
                session_id="session",
                run_id="run-new",
                role="assistant",
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="model"),
                created_at=1,
            ),
            parts=[
                ProviderStatePart(
                    id="state",
                    message_id="assistant",
                    session_id="session",
                    provider="openai_responses",
                    data={
                        "type": "reasoning",
                        "id": "rs-rejected",
                        "encrypted_content": "invalid",
                        "_nexa_rejected": True,
                    },
                ),
                TextPart(
                    id="answer",
                    message_id="assistant",
                    session_id="session",
                    text="fallback answer",
                ),
            ],
        )

        loop = object.__new__(SessionLoop)
        projected = loop._to_openai_messages(
            [message], provider_state_run_id="run-new"
        )

        self.assertEqual(projected, [{"role": "assistant", "content": "fallback answer"}])

    def test_quarantined_state_is_excluded_from_mixed_round_replay(self) -> None:
        message = MessageWithParts(
            info=Message(
                id="assistant",
                session_id="session",
                run_id="run-new",
                role="assistant",
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="model"),
                created_at=1,
            ),
            parts=[
                ProviderStatePart(
                    id="rejected",
                    message_id="assistant",
                    session_id="session",
                    provider="openai_responses",
                    data={
                        "type": "reasoning",
                        "id": "rs-rejected",
                        "encrypted_content": "invalid",
                        "_nexa_rejected": True,
                    },
                ),
                ProviderStatePart(
                    id="valid",
                    message_id="assistant",
                    session_id="session",
                    provider="openai_responses",
                    data={
                        "type": "reasoning",
                        "id": "rs-valid",
                        "encrypted_content": "opaque",
                    },
                ),
                TextPart(
                    id="answer",
                    message_id="assistant",
                    session_id="session",
                    text="done",
                ),
            ],
        )

        loop = object.__new__(SessionLoop)
        projected = loop._to_openai_messages(
            [message], provider_state_run_id="run-new"
        )

        self.assertEqual(
            projected[0]["provider_state"],
            [{"type": "reasoning", "id": "rs-valid", "encrypted_content": "opaque"}],
        )
        self.assertNotIn("_nexa_rejected", str(projected))


class FakeLLMWithReasoning:
    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[list[dict]] = []

    async def stream(self, *, system: str, messages: list[dict], tools: list[dict], params=None, headers=None, **_kwargs):
        self.calls += 1
        self.inputs.append(copy.deepcopy(messages))
        if self.calls == 1:
            yield ReasoningDelta(type="reasoning_delta", text="Need tool. ")
            yield ProviderState(
                type="provider_state",
                provider="openai_responses",
                data={"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
            )
            yield ToolCall(type="tool_call", call_id="call1", name="echo", args_json=json.dumps({"x": 1}))
            yield Finish(type="finish", reason="tool_calls")
            return
        yield Finish(type="finish", reason="stop")


class EchoTool:
    name = "echo"
    description = "echo"

    def schema(self):
        return {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}

    async def execute(self, args, ctx):
        return ToolResult(title="echo", output=str(args.get("x")), metadata={})


class ReasoningToolCallHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_content_is_kept_on_assistant_tool_call_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            sid = "s1"
            now = 1
            session = Session(
                id=sid,
                title="t",
                worktree=tmp,
                cwd=tmp,
                created_at=now,
                updated_at=now,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
            )
            await store.create_session(session)

            user = Message(
                id="u1",
                session_id=sid,
                role="user",
                parent_id=None,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="x"),
                created_at=now,
            )
            await store.add_message(user)
            await store.add_part(sid, user.id, TextPart(id="p1", message_id=user.id, session_id=sid, text="hi"))

            llm = FakeLLMWithReasoning()
            bus = Bus()
            hooks = Hooker()
            perm = PermissionService(bus, store, hooks)
            tools = ToolRegistry([EchoTool()])
            cfg = Config(
                openai=OpenAIConfig(base_url="http://local", api_key="k", model="m"),
                langfuse=LangfuseConfig(
                    enabled=False,
                    public_key="",
                    secret_key="",
                    base_url="https://cloud.langfuse.com",
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
                vlm=VLMConfig(backend="none", api_url="", api_key="", poll_interval=5, timeout=1800),
                logging=LoggingConfig(level="INFO", console=True, file=False, dir="./data/logs", rotation="00:00", retention="7 days"),
                hooks=HooksConfig(debug=False),
                web_search=WebSearchConfig(tavily_api_key=""),
                system_prompt="sys",
                db_path=db,
                default_worktree=tmp,
                default_permission_action="ask",
                prompt_templates={},
            )
            loop = SessionLoop(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                tools=tools,
                llm=llm,
                interrupt=InterruptManager(),
                hooks=hooks,
            )

            await loop.run(session_id=sid)

            self.assertEqual(llm.calls, 2)
            second_call_messages = llm.inputs[1]
            assistant = next(m for m in second_call_messages if m.get("role") == "assistant")
            self.assertIn("tool_calls", assistant)
            self.assertEqual(assistant.get("reasoning_content"), "Need tool. ")
            self.assertEqual(
                assistant.get("provider_state"),
                [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}],
            )

            history = await store.list_messages(sid)
            assistant_history = next(m for m in history if m.info.role == "assistant")
            reasoning_parts = [p for p in assistant_history.parts if getattr(p, "type", "") == "reasoning"]
            self.assertEqual(len(reasoning_parts), 1)
            self.assertEqual(reasoning_parts[0].text, "Need tool. ")
            provider_parts = [p for p in assistant_history.parts if getattr(p, "type", "") == "provider_state"]
            self.assertEqual(len(provider_parts), 1)
            self.assertEqual(provider_parts[0].data["encrypted_content"], "opaque")

            runs = await store.list_runs(sid)
            self.assertEqual(len(runs), 1)
            run = runs[0]
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.finish_reason, "stop")
            self.assertEqual(run.model_rounds, 2)
            self.assertEqual(run.tool_call_count, 1)
            self.assertEqual(run.input_sequence, 1)
            self.assertEqual(run.output_sequence, 3)
            self.assertEqual(run.assistant_message_id, assistant_history.info.id)
            self.assertEqual({message.info.run_id for message in history}, {run.id})
            outbox = await store.list_outbox_events(status="pending")
            self.assertEqual(len(outbox), 1)
            self.assertEqual(outbox[0].event_type, "run.completed")
            self.assertEqual(outbox[0].run_id, run.id)
            self.assertEqual(outbox[0].sequence_from, 1)
            self.assertEqual(outbox[0].sequence_to, 3)
            self.assertEqual(outbox[0].payload["status"], "completed")

            operations = await store.list_tool_operations(sid)
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["tool_call_id"], "call1")
            self.assertEqual(operations[0]["status"], "completed")
            self.assertEqual(operations[0]["result"]["metadata"]["operation_id"], operations[0]["operation_id"])

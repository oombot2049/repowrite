from __future__ import annotations

import unittest
from types import SimpleNamespace

from nexapilot.config import _build_config
from nexapilot.llm.openai_chat import (
    Finish,
    ProviderState,
    ReasoningDelta,
    ResponseStarted,
    TextDelta,
    ToolCall,
)
from nexapilot.llm.errors import ProviderProtocolError
from nexapilot.llm.openai_responses import OpenAIResponsesProvider, _responses_input, _responses_tools


class _FakeStream:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def __aiter__(self):
        self._iterator = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeResponses:
    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._events)


class OpenAIResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_maps_reasoning_text_and_function_call(self) -> None:
        responses = _FakeResponses(
            [
                {"type": "response.reasoning_summary_text.delta", "delta": "Checking tools."},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "opaque",
                        "summary": [],
                    },
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "read",
                        "arguments": '{"path":"README.md"}',
                    },
                },
                {"type": "response.completed", "response": {"id": "resp_1"}},
            ]
        )
        provider = OpenAIResponsesProvider(
            base_url="http://local.test/v1",
            api_key="test-key",
            model="gpt-5.6-luna",
            reasoning_effort="medium",
            client=SimpleNamespace(responses=responses),
        )

        events = [
            event
            async for event in provider.stream(
                system="system",
                messages=[{"role": "user", "content": "inspect"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "read",
                            "description": "Read a file",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                model="override-model",
                run_id="run-1",
                step_id="step-1",
                parent_call_id="parent-call",
                retry_reason="retry",
            )
        ]

        self.assertIsInstance(events[0], ResponseStarted)
        self.assertIsInstance(events[1], ReasoningDelta)
        self.assertIsInstance(events[2], ProviderState)
        self.assertEqual(events[2].data["encrypted_content"], "opaque")
        self.assertIsInstance(events[3], ToolCall)
        self.assertEqual(events[3].call_id, "call_1")
        self.assertEqual(events[3].args_json, '{"path":"README.md"}')
        self.assertEqual(events[4], Finish(type="finish", reason="tool_calls"))
        self.assertEqual(responses.kwargs["reasoning"], {"effort": "medium", "summary": "auto"})
        self.assertEqual(responses.kwargs["include"], ["reasoning.encrypted_content"])
        self.assertFalse(responses.kwargs["store"])
        self.assertEqual(responses.kwargs["tools"][0]["name"], "read")
        self.assertEqual(responses.kwargs["model"], "override-model")

    async def test_stream_maps_output_text(self) -> None:
        responses = _FakeResponses(
            [
                {"type": "response.output_text.delta", "delta": "done"},
                {"type": "response.completed", "response": {"id": "resp_2"}},
            ]
        )
        provider = OpenAIResponsesProvider(
            base_url="http://local.test/v1",
            api_key="test-key",
            model="gpt-5.6-luna",
            client=SimpleNamespace(responses=responses),
        )

        events = [
            event
            async for event in provider.stream(
                system="s",
                messages=[{"role": "user", "content": "respond"}],
                tools=[],
            )
        ]

        self.assertEqual(
            events,
            [
                ResponseStarted(
                    type="response_started", provider_request_id=None
                ),
                TextDelta(type="text_delta", text="done"),
                Finish(type="finish", reason="stop"),
            ],
        )

    async def test_stream_rejects_empty_projected_input_before_http(self) -> None:
        responses = _FakeResponses([])
        provider = OpenAIResponsesProvider(
            base_url="http://local.test/v1",
            api_key="test-key",
            model="gpt-5.6-luna",
            client=SimpleNamespace(responses=responses),
        )

        with self.assertRaisesRegex(
            ProviderProtocolError, "no replayable input"
        ):
            _ = [
                event
                async for event in provider.stream(
                    system="s", messages=[], tools=[]
                )
            ]
        self.assertIsNone(responses.kwargs)

    async def test_stream_rejects_empty_content_message_before_http(self) -> None:
        responses = _FakeResponses([])
        provider = OpenAIResponsesProvider(
            base_url="http://local.test/v1",
            api_key="test-key",
            model="gpt-5.6-luna",
            client=SimpleNamespace(responses=responses),
        )

        with self.assertRaisesRegex(
            ProviderProtocolError, "no replayable input"
        ):
            async for _ in provider.stream(
                system="system",
                messages=[{"role": "user", "content": ""}],
                tools=[],
            ):
                pass

        self.assertIsNone(responses.kwargs)


class OpenAIResponsesConversionTests(unittest.TestCase):
    def test_history_replays_reasoning_calls_and_outputs(self) -> None:
        result = _responses_input(
            [
                {"role": "user", "content": "inspect"},
                {
                    "role": "assistant",
                    "content": "",
                    "provider_state": [
                        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                    ],
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
            ]
        )

        self.assertEqual(result[1]["type"], "reasoning")
        self.assertEqual(result[2]["type"], "function_call")
        self.assertEqual(result[3], {"type": "function_call_output", "call_id": "call_1", "output": "contents"})

    def test_tool_schema_is_flattened_for_responses(self) -> None:
        tools = _responses_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "Read",
                        "parameters": {"type": "object"},
                    },
                }
            ]
        )

        self.assertEqual(
            tools,
            [
                {
                    "type": "function",
                    "name": "read",
                    "description": "Read",
                    "parameters": {"type": "object"},
                    "strict": False,
                }
            ],
        )

    def test_config_selects_responses_and_reasoning_effort(self) -> None:
        cfg = _build_config(
            {
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-5.6-luna",
                    "transport": "responses",
                    "reasoning_effort": "high",
                }
            }
        )

        self.assertEqual(cfg.openai.transport, "responses")
        self.assertEqual(cfg.openai.reasoning_effort, "high")

    def test_config_rejects_invalid_transport_and_timeout_relationship(self) -> None:
        base = {
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model": "gpt-5.6-luna",
            }
        }
        with self.assertRaisesRegex(RuntimeError, "openai.transport"):
            _build_config(
                {"openai": {**base["openai"], "transport": "magic"}}
            )
        with self.assertRaisesRegex(RuntimeError, "total_attempt_ms"):
            _build_config(
                {
                    "openai": {
                        **base["openai"],
                        "resilience": {
                            "timeout": {
                                "connect_ms": 1_000,
                                "first_event_ms": 2_000,
                                "idle_stream_ms": 3_000,
                                "total_attempt_ms": 2_000,
                            }
                        },
                    }
                }
            )

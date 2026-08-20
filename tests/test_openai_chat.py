from __future__ import annotations

import unittest
from types import SimpleNamespace

from nexapilot.llm.openai_chat import Finish, OpenAIChatProvider


class FakeStream:
    def __init__(self) -> None:
        self._events = iter(
            [
                SimpleNamespace(
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content=None,
                                reasoning=None,
                                tool_calls=None,
                            ),
                        )
                    ],
                )
            ]
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return FakeStream()


class OpenAIChatProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_provider_accepts_runtime_metadata_and_model_override(
        self,
    ) -> None:
        provider = OpenAIChatProvider(
            base_url="http://local.test/v1",
            api_key="test-key",
            model="default-model",
        )
        completions = FakeCompletions()
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        events = [
            event
            async for event in provider.stream(
                system="system",
                messages=[],
                tools=[],
                model="override-model",
                run_id="run-1",
                step_id="step-1",
                parent_call_id="parent-call",
                retry_reason="retry",
            )
        ]

        self.assertEqual(events[-1], Finish(type="finish", reason="stop"))
        self.assertEqual(completions.kwargs["model"], "override-model")

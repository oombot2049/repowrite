from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from nexapilot.llm.openai_chat import LLMEvent


class LLMProvider(Protocol):
    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        langfuse_trace_id: str | None = None,
        langfuse_parent_observation_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
        parent_call_id: str | None = None,
        retry_reason: str | None = None,
    ) -> AsyncIterator[LLMEvent]: ...

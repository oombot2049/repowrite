from __future__ import annotations

import copy
import time
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from nexapilot.llm.openai_chat import (
    Finish,
    LLMEvent,
    ProviderState,
    ReasoningDelta,
    ResponseStarted,
    TextDelta,
    ToolCall,
    Usage,
)
from nexapilot.llm.errors import ProviderProtocolError, ProviderUpstreamError
from nexapilot.log import logger


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(exclude_none=True)
        return result if isinstance(result, dict) else {}
    return {}


def _responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            converted.append(copy.deepcopy(tool))
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            converted.append(copy.deepcopy(tool))
            continue
        converted.append(
            {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": copy.deepcopy(function.get("parameters") or {}),
                "strict": False,
            }
        )
    return converted


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "tool":
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue

        if role == "assistant":
            provider_items = message.get("provider_state")
            if isinstance(provider_items, list):
                for item in provider_items:
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        converted.append(copy.deepcopy(item))

            content = message.get("content")
            if isinstance(content, str) and content:
                converted.append({"role": "assistant", "content": content})

            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or ""),
                        }
                    )
            continue

        if role in ("user", "system", "developer"):
            converted.append({"role": role, "content": message.get("content") or ""})
    return converted


def _has_meaningful_input(items: list[dict[str, Any]]) -> bool:
    for item in items:
        if item.get("type") in {"reasoning", "function_call"}:
            return True
        for key in ("content", "output", "arguments"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


class OpenAIResponsesProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str = "medium",
        client: Any | None = None,
    ) -> None:
        self._client = client or AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
        )
        self._model = model
        self._reasoning_effort = reasoning_effort

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
        cancel_check: Any | None = None,
        parent_call_id: str | None = None,
        retry_reason: str | None = None,
    ) -> AsyncIterator[LLMEvent]:
        del (
            langfuse_trace_id,
            langfuse_parent_observation_id,
            run_id,
            step_id,
            cancel_check,
            parent_call_id,
            retry_reason,
        )
        options = (params or {}).get("options")
        request_options = dict(options) if isinstance(options, dict) else {}
        for unsupported in ("temperature", "top_p", "reasoning_effort"):
            request_options.pop(unsupported, None)
        if headers:
            request_options["extra_headers"] = headers

        request_started = time.perf_counter()
        request_model = model or self._model
        request_input = _responses_input(messages)
        if not _has_meaningful_input(request_input):
            raise ProviderProtocolError(
                "Responses request has no replayable input after context projection",
                code="responses_input_empty",
            )
        logger.debug(
            "Creating OpenAI Responses streaming request",
            event="llm.responses.request.start",
            model=request_model,
            reasoning_effort=self._reasoning_effort,
            system_chars=len(system),
            messages_count=len(messages),
            tools_count=len(tools),
        )
        try:
            stream = await self._client.responses.create(
                model=request_model,
                instructions=system,
                input=request_input,
                tools=_responses_tools(tools),
                tool_choice="auto",
                reasoning={"effort": self._reasoning_effort, "summary": "auto"},
                include=["reasoning.encrypted_content"],
                store=False,
                stream=True,
                **request_options,
            )
        except Exception as exc:
            logger.error(
                "Failed to create OpenAI Responses streaming request",
                event="llm.responses.request.error",
                exc_info=exc,
                model=request_model,
                duration_ms=float((time.perf_counter() - request_started) * 1000),
            )
            raise

        stream_request_id = getattr(stream, "_request_id", None)
        yield ResponseStarted(
            type="response_started",
            provider_request_id=str(stream_request_id)
            if stream_request_id
            else None,
        )

        tool_call_count = 0
        terminal_emitted = False
        try:
            async for event in stream:
                event_type = str(_field(event, "type", ""))
                if event_type == "response.output_text.delta":
                    delta = str(_field(event, "delta", "") or "")
                    if delta:
                        yield TextDelta(type="text_delta", text=delta)
                    continue
                if event_type == "response.reasoning_summary_text.delta":
                    delta = str(_field(event, "delta", "") or "")
                    if delta:
                        yield ReasoningDelta(type="reasoning_delta", text=delta)
                    continue
                if event_type == "response.output_item.done":
                    item = _field(event, "item")
                    item_type = str(_field(item, "type", ""))
                    if item_type == "reasoning":
                        data = _as_dict(item)
                        if data:
                            yield ProviderState(type="provider_state", provider="openai_responses", data=data)
                    elif item_type == "function_call":
                        tool_call_count += 1
                        yield ToolCall(
                            type="tool_call",
                            call_id=str(_field(item, "call_id", "") or ""),
                            name=str(_field(item, "name", "") or ""),
                            args_json=str(_field(item, "arguments", "") or ""),
                        )
                    continue
                if event_type == "response.completed":
                    response = _field(event, "response", event)
                    provider_request_id = _field(response, "id") or stream_request_id
                    usage = _field(response, "usage")
                    if usage is not None:
                        input_details = _field(usage, "input_tokens_details", {})
                        output_details = _field(usage, "output_tokens_details", {})
                        yield Usage(
                            type="usage",
                            input_tokens=_field(usage, "input_tokens"),
                            output_tokens=_field(usage, "output_tokens"),
                            cached_tokens=_field(input_details, "cached_tokens"),
                            reasoning_tokens=_field(
                                output_details, "reasoning_tokens"
                            ),
                            provider_request_id=str(provider_request_id)
                            if provider_request_id
                            else None,
                        )
                    terminal_emitted = True
                    yield Finish(type="finish", reason="tool_calls" if tool_call_count else "stop")
                    continue
                if event_type in ("response.failed", "response.incomplete", "error"):
                    response = _field(event, "response", event)
                    error = _field(response, "error") or _field(event, "error")
                    message = _field(error, "message") if error else None
                    code = (
                        _field(error, "code")
                        if error
                        else _field(_field(response, "incomplete_details", {}), "reason")
                    )
                    raise ProviderUpstreamError(
                        str(message or event_type),
                        code=str(code) if code else event_type,
                        request_id=str(_field(response, "id") or stream_request_id or "")
                        or None,
                    )
        except Exception as exc:
            logger.error(
                "Failed while reading OpenAI Responses stream",
                event="llm.responses.stream.error",
                exc_info=exc,
                model=request_model,
            )
            raise

        if not terminal_emitted:
            raise ProviderProtocolError(
                "responses stream ended without a terminal event",
                code="provider_stream_missing_terminal",
            )

        logger.debug(
            "OpenAI Responses stream finished",
            event="llm.responses.stream.finish",
            model=request_model,
            tool_calls_count=tool_call_count,
            duration_ms=float((time.perf_counter() - request_started) * 1000),
        )

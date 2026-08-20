from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

from openai import AsyncOpenAI as StandardAsyncOpenAI

from nexapilot.llm.errors import ProviderProtocolError
from nexapilot.log import logger


@dataclass(frozen=True)
class TextDelta:
    type: str
    text: str


@dataclass(frozen=True)
class ToolCall:
    type: str
    call_id: str
    name: str
    args_json: str


@dataclass(frozen=True)
class ReasoningDelta:
    type: str
    text: str


@dataclass(frozen=True)
class Finish:
    type: str
    reason: str


@dataclass(frozen=True)
class Error:
    type: str
    message: str


@dataclass(frozen=True)
class ResponseStarted:
    type: str
    provider_request_id: str | None = None


@dataclass(frozen=True)
class Usage:
    type: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class ProviderState:
    type: str
    provider: str
    data: dict[str, Any]


LLMEvent = (
    TextDelta
    | ReasoningDelta
    | ToolCall
    | Finish
    | Error
    | ProviderState
    | ResponseStarted
    | Usage
)


def _extract_delta_field(delta: Any, field: str) -> Any:
    direct = getattr(delta, field, None)
    if direct is not None:
        return direct
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get(field)
    return None


def _coerce_reasoning_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "reasoning_content", "content"):
            out = _coerce_reasoning_text(value.get(key))
            if out:
                return out
        return ""
    if isinstance(value, list):
        return "".join(_coerce_reasoning_text(item) for item in value)
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    content_attr = getattr(value, "content", None)
    if content_attr is not None:
        return _coerce_reasoning_text(content_attr)
    return ""


def _extract_reasoning_text(delta: Any) -> str:
    for field in ("reasoning_content", "reasoning"):
        text = _coerce_reasoning_text(_extract_delta_field(delta, field))
        if text:
            return text
    return ""


class OpenAIChatProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str, langfuse_enabled: bool = False) -> None:
        # NexaPilot owns retry accounting. Hidden SDK retries would make one
        # recorded Attempt perform multiple upstream requests.
        client_type = StandardAsyncOpenAI
        langfuse_available = False
        if langfuse_enabled:
            try:
                # Importing langfuse.openai installs process-wide OpenAI
                # instrumentation. Keep the import lazy so a disabled
                # integration cannot emit authentication noise for the plain
                # Responses client.
                from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI

                client_type = LangfuseAsyncOpenAI
                langfuse_available = True
            except ImportError:
                logger.warning(
                    "Langfuse requested but not available; using standard OpenAI client",
                    event="llm.langfuse.unavailable",
                )
        self._client = client_type(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
        )
        self._model = model
        self._langfuse_enabled = langfuse_enabled and langfuse_available

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
        del run_id, step_id, cancel_check, parent_call_id, retry_reason
        opts = params or {}
        options = opts.get("options")
        extra = options if isinstance(options, dict) else {}
        temperature = opts.get("temperature")
        top_p = opts.get("top_p")
        req_started = time.perf_counter()

        kw: dict[str, Any] = {**extra}
        if isinstance(temperature, (int, float)):
            kw["temperature"] = float(temperature)
        if isinstance(top_p, (int, float)):
            kw["top_p"] = float(top_p)
        if headers:
            kw["extra_headers"] = headers

        # Add Langfuse context to properly nest the OpenAI generation under the session trace.
        # The Langfuse OpenAI integration expects the kwargs `trace_id` and `parent_observation_id`
        # (see `langfuse.openai.OpenAiArgsExtractor`), so we map our internal names to those.
        if self._langfuse_enabled:
            if langfuse_trace_id:
                kw["trace_id"] = langfuse_trace_id
            if langfuse_parent_observation_id:
                kw["parent_observation_id"] = langfuse_parent_observation_id

        request_model = model or self._model
        logger.debug(
            "Creating OpenAI-compatible streaming request",
            event="llm.provider.request.start",
            model=request_model,
            system_chars=len(system),
            messages_count=len(messages),
            tools_count=len(tools),
            headers_count=len(headers or {}),
            temperature=kw.get("temperature"),
            top_p=kw.get("top_p"),
            langfuse_trace_attached=bool(langfuse_trace_id),
            langfuse_parent_attached=bool(langfuse_parent_observation_id),
        )

        try:
            kw.setdefault("stream_options", {"include_usage": True})
            stream = await self._client.chat.completions.create(
                model=request_model,
                messages=[{"role": "system", "content": system}, *messages],
                tools=tools,
                tool_choice="auto",
                stream=True,
                **kw,
            )
        except Exception as e:
            logger.error(
                "Failed to create OpenAI-compatible streaming request",
                event="llm.provider.request.error",
                exc_info=e,
                model=request_model,
                duration_ms=float((time.perf_counter() - req_started) * 1000),
            )
            raise
        logger.debug(
            "OpenAI-compatible streaming request created",
            event="llm.provider.request.ready",
            model=request_model,
            duration_ms=float((time.perf_counter() - req_started) * 1000),
        )
        provider_request_id = getattr(stream, "_request_id", None)
        yield ResponseStarted(
            type="response_started",
            provider_request_id=str(provider_request_id)
            if provider_request_id
            else None,
        )

        calls: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None
        chunk_count = 0
        text_delta_count = 0
        tool_delta_count = 0
        stream_started = time.perf_counter()

        try:
            async for chunk in stream:
                chunk_count += 1
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_details = getattr(usage, "input_tokens_details", None)
                    output_details = getattr(usage, "output_tokens_details", None)
                    yield Usage(
                        type="usage",
                        input_tokens=getattr(
                            usage,
                            "prompt_tokens",
                            getattr(usage, "input_tokens", None),
                        ),
                        output_tokens=getattr(
                            usage,
                            "completion_tokens",
                            getattr(usage, "output_tokens", None),
                        ),
                        cached_tokens=getattr(
                            input_details,
                            "cached_tokens",
                            getattr(usage, "cached_tokens", None),
                        ),
                        reasoning_tokens=getattr(
                            output_details,
                            "reasoning_tokens",
                            getattr(usage, "reasoning_tokens", None),
                        ),
                        provider_request_id=str(provider_request_id)
                        if provider_request_id
                        else None,
                    )
                if not chunk.choices:
                    logger.warning(
                        "Received empty choices in streaming chunk",
                        event="llm.provider.chunk.empty",
                        model=request_model,
                        chunk_index=chunk_count,
                    )
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta

                if delta.content:
                    text_delta_count += 1
                    yield TextDelta(type="text_delta", text=delta.content)

                reasoning_text = _extract_reasoning_text(delta)
                if reasoning_text:
                    yield ReasoningDelta(type="reasoning_delta", text=reasoning_text)

                if delta.tool_calls:
                    tool_delta_count += len(delta.tool_calls)
                    for tc in delta.tool_calls:
                        idx = int(tc.index)
                        c = calls.get(idx) or {"id": "", "name": "", "args": ""}
                        if tc.id:
                            c["id"] = tc.id
                        if tc.function and tc.function.name:
                            c["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            c["args"] += tc.function.arguments
                        calls[idx] = c
        except Exception as e:
            logger.error(
                "Failed while reading OpenAI-compatible stream",
                event="llm.provider.stream.error",
                exc_info=e,
                model=request_model,
                chunk_count=chunk_count,
                duration_ms=float((time.perf_counter() - stream_started) * 1000),
            )
            raise

        if not finish_reason:
            raise ProviderProtocolError(
                "chat completions stream ended without a finish reason",
                code="provider_stream_missing_finish",
            )
        yield Finish(type="finish", reason=finish_reason)

        emitted_tool_calls = 0
        if finish_reason == "tool_calls":
            for idx, c in calls.items():
                if c["id"] and c["name"]:
                    emitted_tool_calls += 1
                    yield ToolCall(type="tool_call", call_id=c["id"], name=c["name"], args_json=c["args"])
                    continue
                logger.warning(
                    "Dropped incomplete tool call from streaming response",
                    event="llm.provider.tool_call.incomplete",
                    model=request_model,
                    call_index=idx,
                    has_id=bool(c["id"]),
                    has_name=bool(c["name"]),
                    args_chars=len(c["args"]),
                )

        logger.debug(
            "OpenAI-compatible stream finished",
            event="llm.provider.stream.finish",
            model=request_model,
            finish_reason=finish_reason,
            chunk_count=chunk_count,
            text_delta_count=text_delta_count,
            tool_delta_count=tool_delta_count,
            tool_calls_count=len(calls),
            emitted_tool_calls=emitted_tool_calls,
            duration_ms=float((time.perf_counter() - stream_started) * 1000),
        )

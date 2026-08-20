from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

from nexapilot.bus.bus import Bus, Event
from nexapilot.config import OpenAIConfig
from nexapilot.llm.capabilities import CapabilityResolver, ProviderRequestPlan
from nexapilot.llm.errors import (
    ProviderBudgetExceeded,
    ProviderCallFailed,
    ProviderCircuitOpen,
    ProviderError,
    ProviderErrorCategory,
    ProviderProtocolError,
    ProviderTimeoutError,
    classify_provider_error,
)
from nexapilot.llm.openai_chat import (
    Error,
    LLMEvent,
    ProviderState,
    ReasoningDelta,
    ResponseStarted,
    TextDelta,
    ToolCall,
    Usage,
)
from nexapilot.llm.protocol import LLMProvider
from nexapilot.llm.routing import ModelRouter
from nexapilot.log import logger
from nexapilot.store.sqlite import SQLiteStore

_SEMANTIC_EVENTS = (TextDelta, ReasoningDelta, ToolCall, ProviderState)
_FALLBACK_CATEGORIES = {
    ProviderErrorCategory.RATE_LIMIT,
    ProviderErrorCategory.CONNECTION,
    ProviderErrorCategory.TIMEOUT,
    ProviderErrorCategory.SERVER,
    ProviderErrorCategory.CIRCUIT_OPEN,
}


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _usage_dict(event: Usage | None) -> dict[str, int | None]:
    if event is None:
        return {}
    return {
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cached_tokens": event.cached_tokens,
        "reasoning_tokens": event.reasoning_tokens,
    }


class ProviderGateway:
    def __init__(
        self,
        *,
        config: OpenAIConfig,
        store: SQLiteStore,
        adapters: dict[str, LLMProvider],
        bus: Bus | None = None,
        resolver: CapabilityResolver | None = None,
        router: ModelRouter | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._adapters = adapters
        self._bus = bus
        self._resolver = resolver or CapabilityResolver(config)
        self._router = router
        self._random = random_source or random.Random()

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._resolver.resolve().to_dict()

    @property
    def configured_transport(self) -> str:
        return self._config.transport

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
    ) -> AsyncIterator[LLMEvent]:
        try:
            rejected_candidates: list[dict[str, str]] = []
            if self._router is not None and self._router.enabled:
                route_plan = self._router.plan(
                    system=system,
                    messages=messages,
                    tools=tools,
                    params=params,
                    requested_model=model,
                )
                plans = list(route_plan.candidates)
                rejected_candidates = [
                    {"model_alias": item.model_alias, "reason": item.reason}
                    for item in route_plan.rejected
                ]
            else:
                plans = self._resolver.plans(tools=tools, params=params, model=model)
        except ProviderCallFailed as exc:
            if run_id and step_id:
                await self._record_planning_failure(
                    run_id=run_id,
                    step_id=step_id,
                    system=system,
                    messages=messages,
                    tools=tools,
                    params=params,
                    error=exc.error,
                )
            raise
        except RuntimeError as exc:
            failure = ProviderCallFailed(
                classify_provider_error(
                    ProviderProtocolError(str(exc), code="provider_route_invalid")
                )
            )
            if run_id and step_id:
                await self._record_planning_failure(
                    run_id=run_id,
                    step_id=step_id,
                    system=system,
                    messages=messages,
                    tools=tools,
                    params=params,
                    error=failure.error,
                )
            raise failure from exc

        request_hash = _stable_hash(
            {
                "system": system,
                "messages": messages,
                "tools": tools,
                "model": model or self._config.model,
                "params": params or {},
            }
        )
        endpoint_hash = _stable_hash(self._config.base_url)[:20]
        previous_call_id: str | None = None
        last_failure: ProviderCallFailed | None = None
        attempt_budget = [
            plans[0].max_total_attempts
            if plans and plans[0].max_total_attempts is not None
            else self._config.budgets.max_attempts_per_run
        ]
        if plans and plans[0].route_name:
            await self._publish(
                "llm.route.planned",
                run_id=run_id,
                step_id=step_id,
                route=plans[0].route_name,
                candidates=[
                    {
                        "provider": plan.provider,
                        "model_alias": plan.model_alias,
                        "model": plan.model,
                        "transport": plan.transport,
                    }
                    for plan in plans
                ],
                rejected=rejected_candidates,
                max_total_attempts=attempt_budget[0],
            )

        for plan_index, plan in enumerate(plans):
            if plan_index and (
                last_failure is None
                or last_failure.partial_output
                or not self._fallback_allowed(plan, last_failure.error)
            ):
                break
            if attempt_budget[0] <= 0:
                raise ProviderCallFailed(
                    classify_provider_error(ProviderBudgetExceeded())
                )
            call_id = str(uuid4())
            if run_id and step_id:
                await self._assert_run_budget(run_id)
                await self._store.create_llm_call(
                    call_id=call_id,
                    run_id=run_id,
                    step_id=step_id,
                    provider=plan.provider,
                    endpoint_hash=plan.endpoint_hash or endpoint_hash,
                    model=plan.model,
                    transport=plan.transport,
                    capability_profile_version=plan.capability_profile_version,
                    request_hash=request_hash,
                    parent_call_id=parent_call_id,
                    fallback_from_call_id=previous_call_id,
                    retry_reason=(
                        plan.fallback_reason if plan_index else retry_reason
                    ),
                    metadata={
                        "capability_profile": plan.capability_profile,
                        "reasoning_effort": plan.reasoning_effort,
                        "provider_id": plan.provider_id,
                        "model_alias": plan.model_alias,
                        "route_name": plan.route_name,
                        "fallback_kind": plan.fallback_reason,
                    },
                )
            await self._publish(
                "llm.call.planned",
                run_id=run_id,
                step_id=step_id,
                call_id=call_id,
                model=plan.model,
                transport=plan.transport,
                fallback=bool(plan_index),
                provider=plan.provider,
                model_alias=plan.model_alias,
                route=plan.route_name,
            )
            try:
                async for event in self._run_call(
                    call_id=call_id,
                    run_id=run_id,
                    plan=plan,
                    system=system,
                    messages=messages,
                    tools=tools,
                    params=params,
                    headers=headers,
                    langfuse_trace_id=langfuse_trace_id,
                    langfuse_parent_observation_id=langfuse_parent_observation_id,
                    cancel_check=cancel_check,
                    attempt_budget=attempt_budget,
                ):
                    yield event
                return
            except asyncio.CancelledError:
                if run_id:
                    await self._store.finish_llm_call(
                        call_id,
                        status="cancelled",
                        error_code="provider_cancelled",
                        public_error="The model request was cancelled.",
                    )
                raise
            except ProviderCallFailed as exc:
                if exc.call_id is None:
                    exc.call_id = call_id
                last_failure = exc
                previous_call_id = call_id
                if plan_index + 1 < len(plans) and (
                    not exc.partial_output
                    and self._fallback_allowed(plans[plan_index + 1], exc.error)
                ):
                    next_plan = plans[plan_index + 1]
                    if attempt_budget[0] <= 0:
                        budget_error = classify_provider_error(
                            ProviderBudgetExceeded()
                        )
                        await self._publish(
                            "llm.fallback.skipped",
                            run_id=run_id,
                            step_id=step_id,
                            call_id=call_id,
                            to_provider=next_plan.provider,
                            to_model=next_plan.model,
                            reason=budget_error.code,
                        )
                        raise ProviderCallFailed(budget_error) from exc
                    await self._publish(
                        "llm.fallback.selected",
                        run_id=run_id,
                        step_id=step_id,
                        call_id=call_id,
                        from_transport=plan.transport,
                        to_transport=next_plan.transport,
                        from_provider=plan.provider,
                        to_provider=next_plan.provider,
                        from_model=plan.model,
                        to_model=next_plan.model,
                        from_model_alias=plan.model_alias,
                        to_model_alias=next_plan.model_alias,
                        fallback_kind=(
                            "transport"
                            if plan.model_alias == next_plan.model_alias
                            else "model"
                        ),
                        reason=exc.error.code,
                    )
                    continue
                raise

        if last_failure is not None:
            raise last_failure
        raise ProviderProtocolError("provider planner produced no executable plan")

    async def _run_call(
        self,
        *,
        call_id: str,
        run_id: str | None,
        plan: ProviderRequestPlan,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, object] | None,
        headers: dict[str, str] | None,
        langfuse_trace_id: str | None,
        langfuse_parent_observation_id: str | None,
        cancel_check: Callable[[], Awaitable[bool]] | None,
        attempt_budget: list[int],
    ) -> AsyncIterator[LLMEvent]:
        retry = self._config.resilience.retry
        circuit = self._config.resilience.circuit_breaker
        adapter = self._adapters.get(plan.adapter_key or plan.transport)
        if adapter is None:
            raise ProviderCallFailed(
                classify_provider_error(
                    ProviderProtocolError(
                        "provider adapter is not configured: "
                        f"{plan.adapter_key or plan.transport}",
                        code="provider_adapter_missing",
                    )
                )
            )
        circuit_key = _stable_hash(
            {
                "endpoint_hash": plan.endpoint_hash
                or _stable_hash(self._config.base_url)[:20],
                "model": plan.model,
                "transport": plan.transport,
            }
        )

        for attempt_no in range(1, retry.max_attempts + 1):
            if attempt_budget[0] <= 0:
                error = classify_provider_error(ProviderBudgetExceeded())
                if run_id:
                    await self._store.finish_llm_call(
                        call_id,
                        status="failed",
                        error_code=error.code,
                        public_error=error.public_message,
                    )
                raise ProviderCallFailed(error)
            attempt_budget[0] -= 1
            await self._raise_if_cancelled(cancel_check)
            if run_id:
                await self._assert_run_budget(run_id, check_calls=False)
            attempt_id = str(uuid4())
            if circuit.enabled:
                allowed = await self._store.acquire_provider_circuit(
                    circuit_key=circuit_key,
                    owner_id=attempt_id,
                    now_ms=self._now_ms(),
                )
                if not allowed:
                    error = classify_provider_error(ProviderCircuitOpen())
                    if run_id:
                        await self._store.finish_llm_call(
                            call_id,
                            status="failed",
                            error_code=error.code,
                            public_error=error.public_message,
                        )
                    raise ProviderCallFailed(error)
            if run_id:
                await self._store.start_llm_attempt(
                    attempt_id=attempt_id,
                    call_id=call_id,
                    attempt=attempt_no,
                )
            await self._publish(
                "llm.attempt.started",
                run_id=run_id,
                call_id=call_id,
                attempt_id=attempt_id,
                attempt=attempt_no,
                transport=plan.transport,
            )

            semantic_output_started = False
            usage_event: Usage | None = None
            provider_request_id: str | None = None
            try:
                raw_stream = adapter.stream(
                    system=system,
                    messages=messages,
                    tools=tools,
                    model=plan.model,
                    params=self._params_for_plan(params, plan),
                    headers=headers,
                    langfuse_trace_id=langfuse_trace_id,
                    langfuse_parent_observation_id=langfuse_parent_observation_id,
                )
                async for event in self._stream_with_deadlines(
                    raw_stream,
                    cancel_check=cancel_check,
                ):
                    if isinstance(event, Error):
                        raise ProviderProtocolError(
                            event.message,
                            code="provider_legacy_error_event",
                        )
                    if isinstance(event, ResponseStarted):
                        provider_request_id = (
                            event.provider_request_id or provider_request_id
                        )
                        if run_id:
                            await self._store.mark_llm_attempt_connected(
                                call_id=call_id,
                                attempt_id=attempt_id,
                                provider_request_id=provider_request_id,
                            )
                    if isinstance(event, Usage):
                        usage_event = event
                        provider_request_id = (
                            event.provider_request_id or provider_request_id
                        )
                    if (
                        isinstance(event, _SEMANTIC_EVENTS)
                        and not semantic_output_started
                    ):
                        semantic_output_started = True
                        if run_id:
                            await self._store.mark_llm_semantic_output(
                                call_id=call_id,
                                attempt_id=attempt_id,
                            )
                    yield event

                usage = _usage_dict(usage_event)
                if run_id:
                    await self._store.finish_llm_attempt(
                        attempt_id,
                        status="completed",
                        provider_request_id=provider_request_id,
                        usage=usage,
                    )
                    cost = self._estimate_cost(usage, pricing=plan.pricing)
                    await self._store.finish_llm_call(
                        call_id,
                        status="completed",
                        provider_request_id=provider_request_id,
                        usage=usage,
                        estimated_cost_microusd=cost,
                        pricing_version=(plan.pricing or self._config.pricing).version,
                    )
                if circuit.enabled:
                    await self._store.record_provider_circuit_success(
                        circuit_key=circuit_key,
                        now_ms=self._now_ms(),
                    )
                await self._publish(
                    "llm.call.completed",
                    run_id=run_id,
                    call_id=call_id,
                    attempt=attempt_no,
                    model=plan.model,
                    transport=plan.transport,
                    usage=usage,
                )
                return
            except asyncio.CancelledError:
                if run_id:
                    await self._store.finish_llm_attempt(
                        attempt_id,
                        status="cancelled",
                        error_code="provider_cancelled",
                    )
                    await self._store.finish_llm_call(
                        call_id,
                        status="cancelled",
                        error_code="provider_cancelled",
                        public_error="The model request was cancelled.",
                    )
                raise
            except BaseException as exc:
                error = classify_provider_error(exc)
                terminal_status = "interrupted" if semantic_output_started else "failed"
                if run_id:
                    await self._store.finish_llm_attempt(
                        attempt_id,
                        status=terminal_status,
                        error_code=error.code,
                        diagnostic_summary=error.diagnostic_summary,
                        http_status=error.http_status,
                        provider_code=error.provider_code,
                        provider_request_id=error.provider_request_id,
                        retry_after_ms=error.retry_after_ms,
                        usage=_usage_dict(usage_event),
                    )
                if circuit.enabled and error.retryable:
                    state = await self._store.record_provider_circuit_failure(
                        circuit_key=circuit_key,
                        threshold=circuit.failure_threshold,
                        window_ms=circuit.failure_window_ms,
                        cooldown_ms=circuit.cooldown_ms,
                        now_ms=self._now_ms(),
                    )
                    if state.get("state") == "open":
                        await self._publish(
                            "llm.circuit.opened",
                            run_id=run_id,
                            call_id=call_id,
                            circuit_key=circuit_key,
                            retry_at=state.get("retry_at"),
                        )

                can_retry = (
                    not semantic_output_started
                    and error.retryable
                    and error.safe_to_retry_before_output
                    and attempt_no < retry.max_attempts
                )
                if can_retry:
                    delay_ms = self._retry_delay_ms(error, attempt_no)
                    if run_id:
                        await self._store.set_llm_call_retrying(call_id)
                    await self._publish(
                        "llm.attempt.retry_scheduled",
                        run_id=run_id,
                        call_id=call_id,
                        attempt=attempt_no,
                        error_code=error.code,
                        delay_ms=delay_ms,
                    )
                    try:
                        await self._sleep_cancellable(delay_ms, cancel_check)
                    except asyncio.CancelledError:
                        if run_id:
                            await self._store.finish_llm_call(
                                call_id,
                                status="cancelled",
                                error_code="provider_cancelled",
                                public_error="The model request was cancelled.",
                            )
                        raise
                    continue

                if run_id:
                    await self._store.finish_llm_call(
                        call_id,
                        status=terminal_status,
                        error_code=error.code,
                        public_error=error.public_message,
                        provider_request_id=error.provider_request_id,
                        usage=_usage_dict(usage_event),
                        estimated_cost_microusd=self._estimate_cost(
                            _usage_dict(usage_event), pricing=plan.pricing
                        ),
                        pricing_version=(plan.pricing or self._config.pricing).version,
                    )
                await self._publish(
                    "llm.call.interrupted"
                    if semantic_output_started
                    else "llm.call.failed",
                    run_id=run_id,
                    call_id=call_id,
                    attempt=attempt_no,
                    error_code=error.code,
                    partial_output=semantic_output_started,
                )
                raise ProviderCallFailed(
                    error,
                    partial_output=semantic_output_started,
                ) from exc

    async def _stream_with_deadlines(
        self,
        stream: AsyncIterator[LLMEvent],
        *,
        cancel_check: Callable[[], Awaitable[bool]] | None,
    ) -> AsyncIterator[LLMEvent]:
        iterator = stream.__aiter__()
        timeout = self._config.resilience.timeout
        started = time.monotonic()
        connected = False
        semantic_started = False
        while True:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            remaining_total = timeout.total_attempt_ms - elapsed_ms
            if remaining_total <= 0:
                raise ProviderTimeoutError("total")
            if not connected:
                phase = "connect"
                phase_timeout = timeout.connect_ms
            elif not semantic_started:
                phase = "first_event"
                phase_timeout = timeout.first_event_ms
            else:
                phase = "idle_stream"
                phase_timeout = timeout.idle_stream_ms
            wait_ms = min(phase_timeout, remaining_total)
            try:
                event = await self._next_with_cancel(
                    iterator,
                    timeout_ms=wait_ms,
                    cancel_check=cancel_check,
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise ProviderTimeoutError(phase) from exc
            if isinstance(event, ResponseStarted):
                connected = True
            elif not connected:
                # Third-party adapters may not emit ResponseStarted. Treat the
                # first real event as proof that the connection succeeded.
                connected = True
            if isinstance(event, _SEMANTIC_EVENTS):
                semantic_started = True
            yield event

    async def _next_with_cancel(
        self,
        iterator: AsyncIterator[LLMEvent],
        *,
        timeout_ms: int,
        cancel_check: Callable[[], Awaitable[bool]] | None,
    ) -> LLMEvent:
        next_task = asyncio.create_task(iterator.__anext__())
        cancel_task: asyncio.Task[bool] | None = None
        if cancel_check is not None:
            cancel_task = asyncio.create_task(self._wait_for_cancel(cancel_check))
        tasks: set[asyncio.Task[Any]] = {next_task}
        if cancel_task is not None:
            tasks.add(cancel_task)
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.001, timeout_ms / 1000),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise TimeoutError
        if cancel_task is not None and cancel_task in done and cancel_task.result():
            next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)
            raise asyncio.CancelledError
        if cancel_task is not None:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        return await next_task

    @staticmethod
    async def _wait_for_cancel(
        cancel_check: Callable[[], Awaitable[bool]],
    ) -> bool:
        while True:
            if await cancel_check():
                return True
            await asyncio.sleep(0.1)

    async def _sleep_cancellable(
        self,
        delay_ms: int,
        cancel_check: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        deadline = time.monotonic() + max(0, delay_ms) / 1000
        while True:
            await self._raise_if_cancelled(cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    @staticmethod
    async def _raise_if_cancelled(
        cancel_check: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        if cancel_check is not None and await cancel_check():
            raise asyncio.CancelledError

    def _retry_delay_ms(self, error: ProviderError, attempt_no: int) -> int:
        retry = self._config.resilience.retry
        if error.retry_after_ms is not None:
            return min(error.retry_after_ms, retry.max_retry_after_ms)
        ceiling = min(retry.max_delay_ms, retry.base_delay_ms * (2 ** (attempt_no - 1)))
        return int(self._random.uniform(0, max(0, ceiling)))

    @staticmethod
    def _fallback_allowed(
        next_plan: ProviderRequestPlan, error: ProviderError
    ) -> bool:
        if next_plan.fallback_categories:
            return error.category.value in next_plan.fallback_categories
        return error.category in _FALLBACK_CATEGORIES

    def _params_for_plan(
        self,
        params: dict[str, object] | None,
        plan: ProviderRequestPlan,
    ) -> dict[str, object] | None:
        if params is None:
            return None
        result = copy.deepcopy(params)
        options = result.get("options")
        if (
            isinstance(options, dict)
            and plan.transport == "chat_completions"
            and plan.reasoning_effort == "none"
        ):
            options.pop("reasoning_effort", None)
        return result

    async def _assert_run_budget(
        self, run_id: str, *, check_calls: bool = True
    ) -> None:
        totals = await self._store.get_run_llm_totals(run_id)
        budget = self._config.budgets
        if check_calls and totals["calls"] >= budget.max_calls_per_run:
            raise ProviderCallFailed(classify_provider_error(ProviderBudgetExceeded()))
        if totals["attempts"] >= budget.max_attempts_per_run:
            raise ProviderCallFailed(classify_provider_error(ProviderBudgetExceeded()))
        if (
            budget.max_input_tokens_per_run is not None
            and totals["input_tokens"] >= budget.max_input_tokens_per_run
        ):
            raise ProviderCallFailed(classify_provider_error(ProviderBudgetExceeded()))
        if (
            budget.max_output_tokens_per_run is not None
            and totals["output_tokens"] >= budget.max_output_tokens_per_run
        ):
            raise ProviderCallFailed(classify_provider_error(ProviderBudgetExceeded()))
        if (
            budget.max_cost_microusd_per_run is not None
            and totals["cost_microusd"] >= budget.max_cost_microusd_per_run
        ):
            raise ProviderCallFailed(classify_provider_error(ProviderBudgetExceeded()))

    def _estimate_cost(
        self,
        usage: dict[str, int | None],
        *,
        pricing: Any | None = None,
    ) -> int | None:
        pricing = pricing or self._config.pricing
        if pricing.input_per_million is None or pricing.output_per_million is None:
            return None
        input_tokens = int(usage.get("input_tokens") or 0)
        cached_tokens = min(
            input_tokens,
            int(usage.get("cached_tokens") or 0),
        )
        output_tokens = int(usage.get("output_tokens") or 0)
        cached_rate = (
            pricing.cached_input_per_million
            if pricing.cached_input_per_million is not None
            else pricing.input_per_million
        )
        # USD per million tokens numerically equals micro-USD per token.
        return round(
            (input_tokens - cached_tokens) * pricing.input_per_million
            + cached_tokens * cached_rate
            + output_tokens * pricing.output_per_million
        )

    async def _record_planning_failure(
        self,
        *,
        run_id: str,
        step_id: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, object] | None,
        error: ProviderError,
    ) -> None:
        call_id = str(uuid4())
        capability = self._resolver.resolve()
        await self._store.create_llm_call(
            call_id=call_id,
            run_id=run_id,
            step_id=step_id,
            provider=capability.provider,
            endpoint_hash=_stable_hash(self._config.base_url)[:20],
            model=self._config.model,
            transport=self._config.transport,
            capability_profile_version=capability.profile_version,
            request_hash=_stable_hash(
                {
                    "system": system,
                    "messages": messages,
                    "tools": tools,
                    "params": params or {},
                }
            ),
            metadata={"planning_failure": True},
        )
        await self._store.finish_llm_call(
            call_id,
            status="failed",
            error_code=error.code,
            public_error=error.public_message,
        )

    async def _publish(self, event_type: str, **properties: Any) -> None:
        if self._bus is not None:
            await self._bus.publish(Event(type=event_type, properties=properties))
        logger.info(event_type, event=event_type, **properties)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

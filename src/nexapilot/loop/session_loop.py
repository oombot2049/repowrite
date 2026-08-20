from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from jsonschema import Draft202012Validator

from nexapilot.agents.types import AgentDefinition, AgentLimits, RunRequest
from nexapilot.artifacts import ArtifactStore
from nexapilot.bus.bus import Bus, Event
from nexapilot.config import Config
from nexapilot.hookdefs import Hook
from nexapilot.hooks import Hooker
from nexapilot.llm.errors import ProviderCallFailed, ProviderErrorCategory
from nexapilot.llm.openai_chat import Error as LLMError
from nexapilot.llm.openai_chat import Finish, ProviderState, ReasoningDelta, TextDelta, ToolCall
from nexapilot.llm.protocol import LLMProvider
from nexapilot.log import logger
from nexapilot.loop.interrupt import InterruptManager
from nexapilot.memory.context import ContextManager
from nexapilot.model import (
    Message,
    MessageWithParts,
    ModelRef,
    PermissionRule,
    ProviderStatePart,
    ReasoningPart,
    Run,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
    ToolStateError,
    ToolStatePending,
    ToolStateRunning,
)
from nexapilot.observability.langfuse_client import get_langfuse
from nexapilot.permission.service import PermissionRejected, PermissionService
from nexapilot.prompts.template import render_prompt
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.tools.base import ApprovalScope, ToolContract, get_tool_contract
from nexapilot.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ToolCtx:
    session_id: str
    message_id: str
    agent: str
    source: str
    bus: Bus
    store: SQLiteStore
    perm: PermissionService
    ruleset: list[PermissionRule]
    tool_part_id: str
    trace_id: str | None = None
    parent_observation_id: str | None = None
    root_session_id: str | None = None
    parent_session_id: str | None = None
    parallel_group_id: str | None = None
    parallel_index: int | None = None
    parallel_size: int | None = None
    approval_scope: ApprovalScope = ApprovalScope.ARGUMENTS

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict[str, Any]) -> None:
        ask_metadata = dict(metadata)
        ask_metadata.setdefault("source", self.source)
        ask_metadata.setdefault("approval_scope", self.approval_scope.value)
        if self.approval_scope is ApprovalScope.ONCE:
            effective_always: list[str] = []
        elif self.approval_scope is ApprovalScope.SESSION:
            effective_always = ["*"]
        else:
            effective_always = always
        await self.perm.ask(
            session_id=self.session_id,
            ruleset=self.ruleset,
            permission=permission,
            patterns=patterns,
            always=effective_always,
            metadata=ask_metadata,
            tool={"message_id": self.message_id, "call_id": self.tool_part_id},
        )

    async def tool_stream_update(self, output: str) -> None:
        await self.bus.publish(
            Event(
                type="message.part.updated",
                properties={
                    "session_id": self.session_id,
                    "message_id": self.message_id,
                    "part": {
                        "type": "tool_stream",
                        "call_id": self.tool_part_id,
                        "output": output,
                    },
                },
            ),
        )


@dataclass(frozen=True)
class _PreparedToolCall:
    index: int
    call_id: str
    tool_name: str
    part_id: str
    args: dict[str, Any]
    start_ms: int
    tool: Any
    contract: ToolContract
    ctx: ToolCtx


@dataclass(frozen=True)
class _ToolExecutionOutcome:
    call_id: str
    tool_name: str
    part_id: str
    input: dict[str, Any]
    title: str
    output: str
    metadata: dict[str, Any]
    start_ms: int
    end_ms: int
    status: str = "completed"
    error_code: str | None = None


class SessionLoop:
    def __init__(
        self,
        *,
        cfg: Config,
        bus: Bus,
        store: SQLiteStore,
        perm: PermissionService,
        tools: ToolRegistry,
        llm: LLMProvider,
        interrupt: InterruptManager,
        hooks: Hooker | None = None,
        agent_definition: AgentDefinition | None = None,
        context_manager: ContextManager | None = None,
        artifact_store: ArtifactStore | None = None,
        owner_id: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._perm = perm
        self._tools = tools
        self._llm = llm
        self._interrupt = interrupt
        self._hooks = hooks
        self._agent_definition = agent_definition
        self._context_manager = context_manager
        self._artifact_store = artifact_store
        self._owner_id = owner_id or f"session-loop:{uuid4()}"
        self._locks: dict[str, asyncio.Lock] = {}

    async def _heartbeat(self, run_id: str, session_id: str) -> None:
        interval = max(0.25, self._cfg.durable_run.heartbeat_interval_ms / 1_000)
        while True:
            await asyncio.sleep(interval)
            alive = await self._store.heartbeat_run(
                run_id,
                owner_id=self._owner_id,
                lease_duration_ms=self._cfg.durable_run.lease_duration_ms,
            )
            if not alive:
                await self._interrupt.interrupt(session_id, reason="run_lease_lost")
                return

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def run(
        self,
        *,
        session_id: str | None = None,
        source: str = "api",
        request: RunRequest | None = None,
    ) -> tuple[str, str | None] | tuple[str, str | None, str | None]:
        if request is None:
            if not session_id:
                raise ValueError("session_id is required when request is not provided")
            agent_name = self._agent_definition.name if self._agent_definition else "primary"
            request = RunRequest(
                session_id=session_id,
                agent_name=agent_name,
                source=source,
                root_session_id=session_id,
                limits=self._agent_definition.limits if self._agent_definition else AgentLimits(),
            )
            assistant_id, trace_id, _finish = await self._run_request(request)
            return assistant_id, trace_id
        return await self._run_request(request)

    async def _run_request(self, request: RunRequest) -> tuple[str, str | None, str | None]:
        run_log = logger.child(
            session_id=request.session_id,
            root_session_id=request.root_session_id or request.session_id,
            parent_session_id=request.parent_session_id,
            agent=request.agent_name,
            trace_id=request.trace_id,
        )
        run_log.debug("Session run requested", event="session.run")
        async with self._lock(request.session_id):
            await self._interrupt.clear(request.session_id)

            langfuse = get_langfuse()
            if not langfuse:
                return await self._run_session_with_trace(request, None, request.trace_id)

            trace_kwargs: dict[str, Any] = {
                "as_type": "span",
                "name": "agent-session",
                "metadata": {
                    "agent": request.agent_name,
                    "session_id": request.session_id,
                    "source": request.source,
                    "root_session_id": request.root_session_id or request.session_id,
                    "parent_session_id": request.parent_session_id,
                    "parent_tool_call_id": request.parent_tool_call_id,
                },
            }
            if request.trace_id:
                trace_kwargs["trace_id"] = request.trace_id
            if request.parent_observation_id:
                trace_kwargs["parent_observation_id"] = request.parent_observation_id

            try:
                with langfuse.start_as_current_observation(**trace_kwargs) as trace_span:
                    trace_id = request.trace_id or getattr(trace_span, "trace_id", None)
                    run_log.child(trace_id=trace_id).debug("Langfuse trace started", event="langfuse.trace.start")
                    return await self._run_session_with_trace(request, trace_span, trace_id)
            except TypeError as exc:
                run_log.warning(
                    "Langfuse observation did not accept nested trace kwargs; falling back to root span",
                    event="langfuse.trace.start.fallback",
                    error=str(exc),
                )
                try:
                    fallback_kwargs = {
                        "as_type": "span",
                        "name": "agent-session",
                        "metadata": trace_kwargs["metadata"],
                    }
                    with langfuse.start_as_current_observation(**fallback_kwargs) as trace_span:
                        trace_id = request.trace_id or getattr(trace_span, "trace_id", None)
                        return await self._run_session_with_trace(request, trace_span, trace_id)
                except Exception as inner_exc:
                    run_log.error("Error creating Langfuse trace span", event="langfuse.trace.start.error", exc_info=inner_exc)
                    return await self._run_session_with_trace(request, None, request.trace_id)
            except Exception as exc:
                run_log.error("Error creating Langfuse trace span", event="langfuse.trace.start.error", exc_info=exc)
                return await self._run_session_with_trace(request, None, request.trace_id)

    async def _run_session_with_trace(
        self,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
    ) -> tuple[str, str | None, str | None]:
        session_id = request.session_id
        agent = request.agent_name
        session_log = logger.child(
            session_id=session_id,
            root_session_id=request.root_session_id or session_id,
            parent_session_id=request.parent_session_id,
            trace_id=trace_id,
            agent=agent,
        )
        busy_published = False
        assistant: Message | None = None
        run: Run | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        buffers: dict[str, TextPart | ReasoningPart | None] = {"text": None, "reasoning": None}
        try:
            session = await self._store.get_session(session_id)
            model = (
                self._agent_definition.model_override
                if self._agent_definition
                and self._agent_definition.model_override
                else ModelRef(
                    provider="openai-compatible", id=self._cfg.openai.model
                )
            )
            session_log.info(
                "Session started",
                event="session.start",
                worktree=session.worktree,
                cwd=session.cwd,
                model=model.id,
            )

            if trace_span:
                try:
                    trace_span.update(
                        metadata={
                            "agent": agent,
                            "worktree": session.worktree,
                            "cwd": session.cwd,
                            "model": model.id,
                            "root_session_id": request.root_session_id or session_id,
                            "parent_session_id": request.parent_session_id,
                        }
                    )
                except Exception as exc:
                    session_log.error("Error updating Langfuse trace metadata", event="langfuse.trace.update.error", exc_info=exc)

            await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "busy"}))
            busy_published = True

            history = await self._store.list_messages(session_id)
            user = next((m for m in reversed(history) if m.info.role == "user"), None)
            if not user:
                raise RuntimeError("no user message")

            run = await self._store.create_run(
                session_id=session_id,
                trigger_message_id=user.info.id,
                source=request.source,
                agent_name=agent,
                model=model,
                max_attempts=self._cfg.durable_run.max_attempts,
            )
            user_sequence = await self._store.attach_message_to_run(user.info.id, run.id)
            run = await self._store.start_run(
                run.id,
                input_sequence=user_sequence,
                owner_id=self._owner_id,
                lease_duration_ms=self._cfg.durable_run.lease_duration_ms,
            )
            await self._bus.publish(
                Event(
                    type="run.state.changed",
                    properties={
                        "session_id": session_id,
                        "run_id": run.id,
                        "from": "queued",
                        "to": run.status,
                        "revision": run.revision,
                        "reason": run.state_reason,
                    },
                )
            )
            if self._cfg.durable_run.enabled:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat(run.id, session_id),
                    name=f"run-heartbeat:{run.id}",
                )

            assistant = Message(
                id=str(uuid4()),
                session_id=session_id,
                run_id=run.id,
                role="assistant",
                parent_id=user.info.id,
                agent=agent,
                model=model,
                created_at=int(time.time() * 1000),
            )
            assistant = await self._store.add_message(assistant)
            await self._bus.publish(
                Event(type="message.updated", properties={"session_id": session_id, "info": assistant.model_dump()})
            )

            tool_infos = self._tools.list()
            tool_specs = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.schema,
                    },
                }
                for tool in tool_infos
            ]

            replay_provider_state_run_id = run.id
            messages = self._to_openai_messages(
                history,
                provider_state_run_id=replay_provider_state_run_id,
            )
            turn_index = 0
            executed_tool_calls = 0
            tool_budget_finalization = False

            while True:
                max_turns = request.limits.max_turns or 0
                if (
                    max_turns > 0
                    and turn_index >= max_turns
                    and not tool_budget_finalization
                ):
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish="max_turns_exceeded",
                    )

                turn_index += 1
                await self._store.increment_run_model_rounds(run.id)
                turn_log = session_log.child(message_id=assistant.id)
                turn_log.debug(
                    "Session turn started",
                    event="session.turn.start",
                    turn=turn_index,
                    history_messages=len(messages),
                )

                if await self._store.is_run_cancel_requested(
                    run.id
                ) or await self._interrupt.is_interrupted(session_id):
                    signal = await self._interrupt.check(session_id)
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=signal.reason if signal else "interrupted",
                    )

                await self._store.checkpoint_run(
                    run.id,
                    owner_id=self._owner_id,
                    checkpoint={
                        "phase": "before_model",
                        "turn": turn_index,
                        "model_rounds": turn_index,
                        "tool_calls": executed_tool_calls,
                        "assistant_message_id": assistant.id,
                    },
                    state_reason="before_model",
                )
                model_step = await self._store.create_run_step(
                    run_id=run.id,
                    kind="model",
                    input_ref=assistant.id,
                    metadata={"turn": turn_index},
                )

                system = await self._build_system_prompt(session, agent, model)
                base_system = system
                tools_for_turn = tool_specs
                if tool_budget_finalization:
                    tools_for_turn = []
                    system = (
                        f"{system}\n\n## Tool budget reached\n"
                        "You have no more tool calls available. Do not request tools. "
                        "Produce the best final answer from the evidence already collected, "
                        "state any uncertainty, and mention that exploration stopped at the "
                        "configured tool budget."
                    )
                context_messages = messages
                if self._context_manager is not None:
                    context_result = await self._context_manager.build(
                        session=session,
                        history=history,
                        system_text=system,
                        max_input_tokens=request.limits.max_input_tokens,
                    )
                    turn_log.info(
                        "Context Manager built provider context",
                        event="context.manager.built",
                        **context_result.stats,
                    )
                    if not self._context_manager.shadow_mode:
                        context_messages = self._to_openai_messages(
                            context_result.history,
                            provider_state_run_id=replay_provider_state_run_id,
                        )
                        if context_result.memory_context:
                            system = f"{system}\n\n## Retrieved Memory\n{context_result.memory_context}"
                msgs = await self._transform_messages(session_id, agent, model, context_messages)
                params, hdrs = await self._build_params_and_headers(session_id, agent, model, user)

                messages_count = len(msgs)
                messages_chars = 0
                for msg in msgs:
                    content = msg.get("content")
                    if isinstance(content, str):
                        messages_chars += len(content)
                turn_log.debug(
                    "LLM request",
                    event="llm.request",
                    system_chars=len(system),
                    messages_count=messages_count,
                    messages_chars=messages_chars,
                    tools_count=len(tools_for_turn),
                    temperature=params.get("temperature"),
                    top_p=params.get("top_p"),
                )

                calls: list[ToolCall] = []
                reason: str | None = None
                interrupted_during_stream = False

                async def provider_cancelled() -> bool:
                    return await self._store.is_run_cancel_requested(
                        run.id
                    ) or await self._interrupt.is_interrupted(session_id)

                async def consume_provider_stream(
                    *,
                    request_system: str,
                    request_messages: list[dict[str, Any]],
                    parent_call_id: str | None = None,
                    retry_reason: str | None = None,
                ) -> None:
                    nonlocal interrupted_during_stream, reason
                    async for evt in self._llm.stream(
                        system=request_system,
                        messages=request_messages,
                        tools=tools_for_turn,
                        model=model.id,
                        params=params,
                        headers=hdrs,
                        langfuse_trace_id=trace_id,
                        langfuse_parent_observation_id=getattr(trace_span, "id", None)
                        or request.parent_observation_id,
                        run_id=run.id,
                        step_id=model_step.id,
                        cancel_check=provider_cancelled,
                        parent_call_id=parent_call_id,
                        retry_reason=retry_reason,
                    ):
                        if await self._store.is_run_cancel_requested(
                            run.id
                        ) or await self._interrupt.is_interrupted(session_id):
                            interrupted_during_stream = True
                            break
                        if isinstance(evt, TextDelta):
                            await self._append_text_delta(
                                assistant.id, session_id, buffers, evt.text
                            )
                            continue
                        if isinstance(evt, ReasoningDelta):
                            await self._append_reasoning_delta(
                                assistant.id, session_id, buffers, evt.text
                            )
                            continue
                        if isinstance(evt, ProviderState):
                            state_part = ProviderStatePart(
                                id=str(uuid4()),
                                message_id=assistant.id,
                                session_id=session_id,
                                provider=evt.provider,
                                data=evt.data,
                            )
                            await self._store.add_part(
                                session_id, assistant.id, state_part
                            )
                            continue
                        if isinstance(evt, ToolCall):
                            turn_log.child(
                                tool_name=evt.name, tool_call_id=evt.call_id
                            ).debug(
                                "LLM emitted tool call",
                                event="llm.tool_call.received",
                                args_json_chars=len(evt.args_json or ""),
                            )
                            calls.append(evt)
                            continue
                        if isinstance(evt, LLMError):
                            turn_log.error(
                                "LLM returned error event",
                                event="llm.response.error",
                                error_message=evt.message,
                            )
                            raise RuntimeError(f"LLM provider error: {evt.message}")
                        if isinstance(evt, Finish):
                            reason = evt.reason
                            await self._flush_buffered_parts(
                                session_id, assistant.id, buffers
                            )
                            turn_log.debug(
                                "LLM finished",
                                event="llm.finish",
                                finish_reason=reason,
                            )

                try:
                    try:
                        await consume_provider_stream(
                            request_system=system,
                            request_messages=msgs,
                        )
                    except ProviderCallFailed as exc:
                        can_rebuild_provider_state = (
                            exc.error.provider_code == "invalid_encrypted_content"
                            and not exc.partial_output
                            and exc.call_id is not None
                        )
                        if can_rebuild_provider_state:
                            quarantined = await self._store.quarantine_provider_state(
                                run.id
                            )
                            # Some OpenAI-compatible endpoints emit opaque
                            # reasoning state that they later reject even for
                            # the same model. Once detected, disable replay for
                            # the remainder of this Run; continuing to persist
                            # new state is useful for diagnostics but it must
                            # not poison every later model round.
                            replay_provider_state_run_id = ""
                            history = await self._store.list_messages(session_id)
                            rebuilt_messages = self._to_openai_messages(
                                history,
                                provider_state_run_id=replay_provider_state_run_id,
                            )
                            rebuilt_messages = await self._transform_messages(
                                session_id,
                                agent,
                                model,
                                rebuilt_messages,
                            )
                            turn_log.warning(
                                "Provider rejected encrypted reasoning state; rebuilding once",
                                event="provider_state.replay_rejected",
                                parent_call_id=exc.call_id,
                                quarantined_parts=quarantined,
                            )
                            await consume_provider_stream(
                                request_system=system,
                                request_messages=rebuilt_messages,
                                parent_call_id=exc.call_id,
                                retry_reason="provider_state_rebuild",
                            )
                        else:
                            can_rebuild_context = (
                                exc.error.category
                                == ProviderErrorCategory.CONTEXT_OVERFLOW
                                and not exc.partial_output
                                and exc.call_id is not None
                                and self._context_manager is not None
                                and not self._context_manager.shadow_mode
                            )
                            if not can_rebuild_context:
                                raise
                            rebuild = await self._context_manager.build(
                                session=session,
                                history=history,
                                system_text=base_system,
                                max_input_tokens=(
                                    self._context_manager.overflow_retry_input_tokens
                                ),
                            )
                            rebuilt_system = base_system
                            if rebuild.memory_context:
                                rebuilt_system = (
                                    f"{base_system}\n\n## Retrieved Memory\n"
                                    f"{rebuild.memory_context}"
                                )
                            rebuilt_messages = self._to_openai_messages(
                                rebuild.history,
                                provider_state_run_id=replay_provider_state_run_id,
                            )
                            rebuilt_messages = await self._transform_messages(
                                session_id,
                                agent,
                                model,
                                rebuilt_messages,
                            )
                            turn_log.warning(
                                "Provider context overflow; rebuilding once",
                                event="context.manager.overflow_rebuild",
                                parent_call_id=exc.call_id,
                                **rebuild.stats,
                            )
                            await consume_provider_stream(
                                request_system=rebuilt_system,
                                request_messages=rebuilt_messages,
                                parent_call_id=exc.call_id,
                                retry_reason="context_rebuild",
                            )
                except asyncio.CancelledError:
                    await self._flush_buffered_parts(session_id, assistant.id, buffers)
                    await self._store.finish_run_step(
                        model_step.id,
                        status="cancelled",
                        error_code="user_cancelled",
                    )
                    signal = await self._interrupt.check(session_id)
                    finish = signal.reason if signal else "interrupted"
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=finish,
                    )
                except ProviderCallFailed as exc:
                    await self._flush_buffered_parts(
                        session_id, assistant.id, buffers
                    )
                    step_status = "interrupted" if exc.partial_output else "failed"
                    await self._store.finish_run_step(
                        model_step.id,
                        status=step_status,
                        output_ref=assistant.id if exc.partial_output else None,
                        error_code=exc.error.code,
                        metadata={
                            "provider_error_category": exc.error.category.value,
                            "partial_output": exc.partial_output,
                            "http_status": exc.error.http_status,
                            "provider_request_id": exc.error.provider_request_id,
                        },
                    )
                    turn_log.error(
                        "LLM provider call failed",
                        event="llm.call.failed",
                        error_code=exc.error.code,
                        error_category=exc.error.category.value,
                        partial_output=exc.partial_output,
                    )
                    public_error = {
                        "message": exc.error.public_message,
                        "type": "ProviderCallFailed",
                        "code": exc.error.code,
                        "category": exc.error.category.value,
                        "partial_output": exc.partial_output,
                    }
                    await self._bus.publish(
                        Event(
                            type="session.error",
                            properties={
                                "session_id": session_id,
                                "error": public_error,
                            },
                        )
                    )
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish="provider_stream_interrupted"
                        if exc.partial_output
                        else "error",
                        error=public_error,
                    )
                except Exception as exc:
                    await self._flush_buffered_parts(session_id, assistant.id, buffers)
                    await self._store.finish_run_step(
                        model_step.id,
                        status="failed",
                        error_code="llm_stream_failed",
                        metadata={"error_type": type(exc).__name__},
                    )
                    turn_log.error("LLM streaming error", event="llm.stream.error", exc_info=exc)
                    await self._bus.publish(
                        Event(
                            type="session.error",
                            properties={
                                "session_id": session_id,
                                "error": {
                                    "message": str(exc),
                                    "type": type(exc).__name__,
                                    "context": "llm_stream",
                                },
                            },
                        )
                    )
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish="error",
                        error={"message": str(exc), "type": type(exc).__name__},
                    )

                if interrupted_during_stream:
                    await self._store.finish_run_step(
                        model_step.id,
                        status="interrupted",
                        output_ref=assistant.id,
                        error_code="run_interrupted",
                    )
                    turn_log.info("LLM stream interrupted by user signal", event="llm.stream.interrupted", turn=turn_index)
                    signal = await self._interrupt.check(session_id)
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=signal.reason if signal else "interrupted",
                    )

                await self._store.finish_run_step(
                    model_step.id,
                    status="completed",
                    output_ref=assistant.id,
                    metadata={
                        "finish_reason": reason,
                        "tool_call_count": len(calls),
                    },
                )

                if calls:
                    max_tool_calls = request.limits.max_tool_calls or 0
                    if max_tool_calls > 0 and executed_tool_calls + len(calls) > max_tool_calls:
                        if tool_budget_finalization:
                            return await self._finish_assistant(
                                session_id=session_id,
                                assistant=assistant,
                                trace_id=trace_id,
                                finish="max_tool_calls_exceeded",
                            )
                        await self._persist_tool_budget_outcomes(
                            session=session,
                            assistant=assistant,
                            model=model,
                            calls=calls,
                            max_tool_calls=max_tool_calls,
                            executed_tool_calls=executed_tool_calls,
                        )
                        await self._bus.publish(
                            Event(
                                type="agent.tool_budget.exhausted",
                                properties={
                                    "session_id": session_id,
                                    "run_id": run.id,
                                    "agent": agent,
                                    "max_tool_calls": max_tool_calls,
                                    "executed_tool_calls": executed_tool_calls,
                                    "rejected_tool_calls": len(calls),
                                    "status": "finalizing",
                                },
                            )
                        )
                        history = await self._store.list_messages(session_id)
                        messages = self._to_openai_messages(
                            history,
                            provider_state_run_id=replay_provider_state_run_id,
                        )
                        tool_budget_finalization = True
                        continue

                    turn_log.debug(
                        "Processing tool calls",
                        event="llm.tool_calls.batch",
                        tool_calls_count=len(calls),
                        finish_reason=reason,
                    )
                    await self._store.checkpoint_run(
                        run.id,
                        owner_id=self._owner_id,
                        checkpoint={
                            "phase": "before_tools",
                            "turn": turn_index,
                            "assistant_message_id": assistant.id,
                            "pending_tool_call_ids": [call.call_id for call in calls],
                            "tool_calls": executed_tool_calls,
                        },
                        state_reason="before_tools",
                    )
                    tool_step = await self._store.create_run_step(
                        run_id=run.id,
                        kind="tool_batch",
                        input_ref=assistant.id,
                        metadata={
                            "turn": turn_index,
                            "tool_call_ids": [call.call_id for call in calls],
                        },
                    )
                    try:
                        blocked = await self._execute_tool_batch(
                            session=session,
                            assistant=assistant,
                            model=model,
                            request=request,
                            trace_span=trace_span,
                            trace_id=trace_id,
                            turn_log=turn_log,
                            calls=calls,
                        )
                    except asyncio.CancelledError:
                        await self._store.finish_run_step(
                            tool_step.id,
                            status="cancelled",
                            error_code="user_cancelled",
                        )
                        raise
                    except Exception as exc:
                        await self._store.finish_run_step(
                            tool_step.id,
                            status="failed",
                            error_code="tool_batch_failed",
                            metadata={"error_type": type(exc).__name__},
                        )
                        raise
                    else:
                        await self._store.finish_run_step(
                            tool_step.id,
                            status="completed",
                            output_ref=assistant.id,
                            metadata={"blocked": blocked},
                        )
                    executed_tool_calls += len(calls)
                    await self._store.increment_run_tool_calls(run.id, len(calls))
                    if blocked:
                        return await self._finish_assistant(
                            session_id=session_id,
                            assistant=assistant,
                            trace_id=trace_id,
                            finish="blocked",
                        )
                    history = await self._store.list_messages(session_id)
                    messages = self._to_openai_messages(
                        history,
                        provider_state_run_id=replay_provider_state_run_id,
                    )
                    await self._store.checkpoint_run(
                        run.id,
                        owner_id=self._owner_id,
                        checkpoint={
                            "phase": "after_tools",
                            "turn": turn_index,
                            "assistant_message_id": assistant.id,
                            "completed_tool_call_ids": [call.call_id for call in calls],
                            "tool_calls": executed_tool_calls,
                        },
                        state_reason="after_tools",
                    )
                    turn_log.debug(
                        "Session turn continuing after tool execution",
                        event="session.turn.next",
                        turn=turn_index,
                        history_messages=len(messages),
                    )
                    continue

                if reason and reason != "tool_calls":
                    turn_log.info("Session finished", event="session.finish", finish_reason=reason, turn=turn_index)
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=reason,
                    )

        except asyncio.CancelledError:
            signal = await self._interrupt.check(session_id)
            finish = signal.reason if signal else "interrupted"
            if run is not None:
                if assistant is not None:
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=finish,
                    )
                await self._store.finish_run(
                    run.id,
                    status="cancelled" if finish == "user_cancelled" else "interrupted",
                    assistant_message_id=None,
                    finish_reason=finish,
                    error_code=finish,
                )
            raise
        except Exception as exc:
            terminal_error = {
                "code": "run_execution_failed",
                "message": str(exc),
                "type": type(exc).__name__,
            }
            if run is not None:
                try:
                    if assistant is not None:
                        await self._finish_assistant(
                            session_id=session_id,
                            assistant=assistant,
                            trace_id=trace_id,
                            finish="error",
                            error=terminal_error,
                        )
                    else:
                        await self._store.finish_run(
                            run.id,
                            status="failed",
                            assistant_message_id=None,
                            finish_reason="error",
                            error=terminal_error,
                            error_code="run_execution_failed",
                        )
                except Exception as finish_exc:
                    session_log.error(
                        "Failed to terminalize Run after execution error",
                        event="run.finish.error",
                        exc_info=finish_exc,
                    )
            if trace_span:
                try:
                    trace_span.update(level="ERROR", metadata={"error_type": type(exc).__name__, "error_message": str(exc)})
                except Exception as inner_exc:
                    session_log.error(
                        "Error updating Langfuse trace with error metadata",
                        event="langfuse.trace.error_update.error",
                        exc_info=inner_exc,
                    )
            session_log.error("Session failed early", event="session.error", exc_info=exc)
            await self._bus.publish(
                Event(
                    type="session.error",
                    properties={"session_id": session_id, "error": {"message": str(exc), "type": type(exc).__name__}},
                )
            )
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            if busy_published:
                await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))

    async def _persist_tool_budget_outcomes(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        calls: list[ToolCall],
        max_tool_calls: int,
        executed_tool_calls: int,
    ) -> None:
        """Close rejected tool calls so provider history remains protocol-valid."""
        for call in calls:
            try:
                parsed = json.loads(call.args_json or "{}")
                args = parsed if isinstance(parsed, dict) else {}
            except Exception:
                args = {}
            part_id = str(uuid4())
            pending = ToolPart(
                id=part_id,
                message_id=assistant.id,
                session_id=session.id,
                call_id=call.call_id,
                tool=call.name,
                state=ToolStatePending(input=args, raw=call.args_json or ""),
            )
            await self._store.add_part(session.id, assistant.id, pending)
            await self._bus.publish(
                Event(
                    type="message.part.updated",
                    properties={
                        "session_id": session.id,
                        "message_id": assistant.id,
                        "part": pending.model_dump(),
                    },
                )
            )
            outcome = self._immediate_tool_outcome(
                call_id=call.call_id,
                tool_name=call.name,
                part_id=part_id,
                input=args,
                output=(
                    "Tool call was not executed because the configured tool budget "
                    f"is exhausted ({executed_tool_calls}/{max_tool_calls}). "
                    "Summarize the evidence already collected without calling more tools."
                ),
                error_code="tool_budget_exhausted",
            )
            await self._persist_tool_outcome(session.id, assistant, model, outcome)

    async def _build_system_prompt(self, session: Session, agent: str, model: ModelRef) -> str:
        prompt_worktree = session.worktree
        if session.runtime.backend == "daytona" and (not prompt_worktree or prompt_worktree == self._cfg.default_worktree):
            prompt_worktree = self._cfg.daytona.default_workspace or "/workspace"

        system = render_prompt(
            self._cfg.system_prompt,
            session_context={
                "session_id": session.id,
                "cwd": session.cwd,
                "worktree": prompt_worktree,
                "model": model.id,
                "agent": agent,
            },
            template_variables=self._cfg.prompt_templates,
        )
        if self._agent_definition:
            extra = self._agent_definition.load_prompt().strip()
            if extra:
                system = f"{system}\n\n{extra}"
        if self._hooks:
            sysout: dict[str, object] = {"system": [system]}
            await self._hooks.trigger(
                Hook.ExperimentalChatSystemTransform,
                {"session_id": session.id, "agent": agent, "model": model.model_dump()},
                sysout,
            )
            raw = sysout.get("system")
            if isinstance(raw, list):
                parts = [item for item in raw if isinstance(item, str)]
                if parts:
                    system = "\n\n".join(parts)
        return system

    async def _transform_messages(
        self,
        session_id: str,
        agent: str,
        model: ModelRef,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = messages
        if self._hooks:
            mout: dict[str, object] = {"messages": msgs}
            await self._hooks.trigger(
                Hook.ExperimentalChatMessagesTransform,
                {"session_id": session_id, "agent": agent, "model": model.model_dump()},
                mout,
            )
            raw = mout.get("messages")
            if isinstance(raw, list):
                msgs = cast(list[dict[str, Any]], raw)
        return msgs

    async def _build_params_and_headers(
        self,
        session_id: str,
        agent: str,
        model: ModelRef,
        user: MessageWithParts,
    ) -> tuple[dict[str, object], dict[str, str]]:
        params: dict[str, object] = {"temperature": 1.0, "top_p": 0.95, "top_k": 0, "options": {}}
        headers: dict[str, object] = {"headers": {}}
        if self._hooks:
            user_text = "".join([part.text for part in user.parts if getattr(part, "type", "") == "text"])
            await self._hooks.trigger(
                Hook.ChatParams,
                {
                    "session_id": session_id,
                    "agent": agent,
                    "model": model.model_dump(),
                    "message_id": user.info.id,
                    "message": user_text,
                },
                params,
            )
            await self._hooks.trigger(
                Hook.ChatHeaders,
                {
                    "session_id": session_id,
                    "agent": agent,
                    "model": model.model_dump(),
                    "message_id": user.info.id,
                    "message": user_text,
                },
                headers,
            )
        raw_headers = headers.get("headers")
        return params, cast(dict[str, str], raw_headers if isinstance(raw_headers, dict) else {})

    async def _append_text_delta(
        self,
        message_id: str,
        session_id: str,
        buffers: dict[str, TextPart | ReasoningPart | None],
        delta: str,
    ) -> None:
        current = cast(TextPart | None, buffers["text"])
        if current is None:
            current = TextPart(
                id=str(uuid4()),
                message_id=message_id,
                session_id=session_id,
                text=delta,
                time={"start": int(time.time() * 1000)},
            )
        else:
            current.text += delta
        buffers["text"] = current
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={
                    "session_id": session_id,
                    "message_id": message_id,
                    "part": current.model_dump(),
                    "delta": delta,
                },
            )
        )

    async def _append_reasoning_delta(
        self,
        message_id: str,
        session_id: str,
        buffers: dict[str, TextPart | ReasoningPart | None],
        delta: str,
    ) -> None:
        current = cast(ReasoningPart | None, buffers["reasoning"])
        if current is None:
            current = ReasoningPart(
                id=str(uuid4()),
                message_id=message_id,
                session_id=session_id,
                text=delta,
                time={"start": int(time.time() * 1000), "end": int(time.time() * 1000)},
            )
        else:
            current.text += delta
            current.time["end"] = int(time.time() * 1000)
        buffers["reasoning"] = current
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={
                    "session_id": session_id,
                    "message_id": message_id,
                    "part": current.model_dump(),
                    "delta": delta,
                },
            )
        )

    async def _flush_buffered_parts(
        self,
        session_id: str,
        message_id: str,
        buffers: dict[str, TextPart | ReasoningPart | None],
    ) -> None:
        reasoning = cast(ReasoningPart | None, buffers["reasoning"])
        if reasoning is not None:
            end_ms = int(time.time() * 1000)
            reasoning.time = {
                "start": reasoning.time.get("start", end_ms),
                "end": end_ms,
            }
            await self._store.add_part(session_id, message_id, reasoning)
            buffers["reasoning"] = None

        text = cast(TextPart | None, buffers["text"])
        if text is not None:
            end_ms = int(time.time() * 1000)
            text.time = {
                "start": (text.time or {}).get("start", end_ms),
                "end": end_ms,
            }
            await self._store.add_part(session_id, message_id, text)
            buffers["text"] = None

    async def _finish_assistant(
        self,
        *,
        session_id: str,
        assistant: Message,
        trace_id: str | None,
        finish: str,
        error: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, str]:
        completed_at = int(time.time() * 1000)
        finished_run: Run | None = None
        if assistant.run_id is not None:
            if error is not None or finish in {"error", "max_turns_exceeded", "max_tool_calls_exceeded"}:
                run_status = "failed"
            elif finish == "user_cancelled":
                run_status = "cancelled"
            elif finish in {
                "interrupted",
                "run_lease_lost",
                "provider_stream_interrupted",
            }:
                run_status = "interrupted"
            else:
                run_status = "completed"
            finished_run, _outbox = await self._store.finish_assistant_run_with_outbox(
                assistant_message_id=assistant.id,
                completed_at=completed_at,
                finish_reason=finish,
                run_status=run_status,
                error=error,
            )
        else:
            await self._store.update_message(assistant.id, completed_at=completed_at, finish=finish, error=error)
        info = assistant.model_dump()
        info["completed_at"] = completed_at
        info["finish"] = finish
        if error is not None:
            info["error"] = error
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": info}))
        if finished_run is not None:
            await self._bus.publish(
                Event(
                    type="run.state.changed",
                    properties={
                        "session_id": session_id,
                        "run_id": finished_run.id,
                        "to": finished_run.status,
                        "revision": finished_run.revision,
                        "reason": finished_run.finish_reason,
                        "error_code": finished_run.error_code,
                    },
                )
            )
        return assistant.id, trace_id, finish

    async def _execute_tool_batch(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
        turn_log: Any,
        calls: list[ToolCall],
    ) -> bool:
        if self._is_parallel_task_batch(calls):
            return await self._execute_parallel_tool_batch(
                session=session,
                assistant=assistant,
                model=model,
                request=request,
                trace_span=trace_span,
                trace_id=trace_id,
                turn_log=turn_log,
                calls=calls,
            )
        return await self._execute_sequential_tool_batch(
            session=session,
            assistant=assistant,
            model=model,
            request=request,
            trace_span=trace_span,
            trace_id=trace_id,
            turn_log=turn_log,
            calls=calls,
        )

    async def _execute_sequential_tool_batch(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
        turn_log: Any,
        calls: list[ToolCall],
    ) -> bool:
        for index, call in enumerate(calls):
            prepared, immediate = await self._prepare_tool_call(
                session=session,
                assistant=assistant,
                request=request,
                trace_id=trace_id,
                turn_log=turn_log,
                call=call,
                index=index,
                parallel_group_id=None,
                parallel_index=None,
                parallel_size=None,
            )
            if immediate is not None:
                await self._persist_tool_outcome(session.id, assistant, model, immediate)
                continue
            if prepared is None:
                continue
            outcome = await self._execute_prepared_tool_call(
                prepared=prepared,
                request=request,
                trace_span=trace_span,
                turn_log=turn_log,
            )
            await self._persist_tool_outcome(session.id, assistant, model, outcome)
        return False

    async def _execute_parallel_tool_batch(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
        turn_log: Any,
        calls: list[ToolCall],
    ) -> bool:
        parallel_group_id = str(uuid4())
        outcomes: list[_ToolExecutionOutcome | BaseException | None] = [None] * len(calls)
        prepared_calls: list[_PreparedToolCall] = []
        parallel_size = len(calls)

        for index, call in enumerate(calls):
            prepared, immediate = await self._prepare_tool_call(
                session=session,
                assistant=assistant,
                request=request,
                trace_id=trace_id,
                turn_log=turn_log,
                call=call,
                index=index,
                parallel_group_id=parallel_group_id,
                parallel_index=index + 1,
                parallel_size=parallel_size,
            )
            if immediate is not None:
                outcomes[index] = immediate
                continue
            if prepared is not None:
                prepared_calls.append(prepared)

        results = await asyncio.gather(
            *[
                self._execute_prepared_tool_call(
                    prepared=prepared,
                    request=request,
                    trace_span=trace_span,
                    turn_log=turn_log,
                )
                for prepared in prepared_calls
            ],
            return_exceptions=True,
        )
        for prepared, result in zip(prepared_calls, results):
            outcomes[prepared.index] = cast(_ToolExecutionOutcome | BaseException, result)

        for outcome in outcomes:
            if outcome is None:
                continue
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                raise outcome
            await self._persist_tool_outcome(session.id, assistant, model, outcome)
        return False

    async def _prepare_tool_call(
        self,
        *,
        session: Session,
        assistant: Message,
        request: RunRequest,
        trace_id: str | None,
        turn_log: Any,
        call: ToolCall,
        index: int,
        parallel_group_id: str | None,
        parallel_index: int | None,
        parallel_size: int | None,
    ) -> tuple[_PreparedToolCall | None, _ToolExecutionOutcome | None]:
        call_id = call.call_id
        tool_name = call.name
        raw = call.args_json
        tool_log = turn_log.child(tool_name=tool_name, tool_call_id=call_id)

        part_id = str(uuid4())
        try:
            args = json.loads(raw or "{}")
        except Exception as exc:
            pending = ToolPart(
                id=part_id,
                message_id=assistant.id,
                session_id=session.id,
                call_id=call_id,
                tool=tool_name,
                state=ToolStatePending(input={}, raw=raw),
            )
            await self._store.add_part(session.id, assistant.id, pending)
            await self._bus.publish(
                Event(
                    type="message.part.updated",
                    properties={"session_id": session.id, "message_id": assistant.id, "part": pending.model_dump()},
                )
            )
            raw_preview = (raw or "")[:200]
            tool_log.error(
                "Failed to parse tool arguments",
                event="tool.args.parse.error",
                exc_info=exc,
                args_json_chars=len(raw or ""),
                args_json_preview=raw_preview,
            )
            return None, self._immediate_tool_outcome(
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                input={},
                output=f"Invalid tool arguments: {exc}",
                error_code="invalid_tool_arguments",
            )

        if not isinstance(args, dict):
            pending = ToolPart(
                id=part_id,
                message_id=assistant.id,
                session_id=session.id,
                call_id=call_id,
                tool=tool_name,
                state=ToolStatePending(input={}, raw=raw),
            )
            await self._store.add_part(session.id, assistant.id, pending)
            await self._bus.publish(
                Event(
                    type="message.part.updated",
                    properties={"session_id": session.id, "message_id": assistant.id, "part": pending.model_dump()},
                )
            )
            return None, self._immediate_tool_outcome(
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                input={},
                output="Invalid tool arguments: top-level value must be an object",
                error_code="invalid_tool_arguments",
            )

        pending = ToolPart(
            id=part_id,
            message_id=assistant.id,
            session_id=session.id,
            call_id=call_id,
            tool=tool_name,
            state=ToolStatePending(input=args, raw=raw),
        )
        await self._store.add_part(session.id, assistant.id, pending)
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session.id, "message_id": assistant.id, "part": pending.model_dump()},
            )
        )

        if self._hooks:
            out: dict[str, object] = {"args": args}
            await self._hooks.trigger(
                Hook.ToolExecuteBefore,
                {"tool": tool_name, "session_id": session.id, "call_id": call_id},
                out,
            )
            raw_args = out.get("args")
            if isinstance(raw_args, dict):
                args = cast(dict[str, Any], raw_args)

        try:
            tool = self._tools.get(tool_name)
        except Exception as exc:
            tool_log.error("Tool lookup failed", event="tool.lookup.error", exc_info=exc)
            return None, self._immediate_tool_outcome(
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                input=args,
                output=f"Tool not found: {tool_name}",
                title=tool_name,
                error_code="tool_not_found",
            )
        contract = get_tool_contract(tool)
        contract_metadata = contract.to_metadata()
        idempotency_key_present = bool(str(args.get("idempotency_key") or "").strip())

        validation_errors = sorted(
            Draft202012Validator(tool.schema()).iter_errors(args),
            key=lambda error: list(error.absolute_path),
        )
        if validation_errors:
            details = "; ".join(error.message for error in validation_errors[:3])
            tool_log.warning(
                "Tool arguments failed schema validation",
                event="tool.args.validation.error",
                validation_error=details,
            )
            return None, self._immediate_tool_outcome(
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                input=args,
                output=f"Invalid tool arguments: {details}",
                error_code="invalid_tool_arguments",
            )

        start_ms = int(time.time() * 1000)
        safe_args: dict[str, Any] = {}
        if isinstance(args, dict):
            safe_args["args_keys"] = list(args.keys())
            safe_args["string_value_chars"] = {
                key: len(value) for key, value in args.items() if isinstance(key, str) and isinstance(value, str)
            }
        tool_log.debug("Tool execution started", event="tool.start", **safe_args)

        running = ToolPart(
            id=part_id,
            message_id=assistant.id,
            session_id=session.id,
            call_id=call_id,
            tool=tool_name,
            state=ToolStateRunning(input=args, time={"start": start_ms}),
        )
        await self._store.add_part(session.id, assistant.id, running)
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session.id, "message_id": assistant.id, "part": running.model_dump()},
            )
        )

        operation_id = str(
            uuid5(
                NAMESPACE_URL,
                f"nexapilot:{assistant.run_id or session.id}:{call_id}",
            )
        )
        capability, canonical_target = await self._describe_tool_operation(
            session.id, tool_name, args
        )
        await self._store.upsert_tool_operation(
            {
                "operation_id": operation_id,
                "run_id": assistant.run_id,
                "session_id": session.id,
                "message_id": assistant.id,
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "capability": capability,
                "canonical_target": canonical_target,
                "executor_backend": "pending",
                "isolation_level": "pending",
                "status": "executing",
                "error_code": None,
                "input": args,
                "result": {
                    "metadata": {
                        "side_effect_state": (
                            "none" if contract.side_effect.value == "none" else "unknown"
                        ),
                        "phase": "before_execution",
                        "tool_contract": contract_metadata,
                        "idempotency_key_present": idempotency_key_present,
                        "recovery_action": contract.recovery_action(
                            idempotency_key_present=idempotency_key_present
                        ),
                    }
                },
                "created_at": start_ms,
                "finished_at": start_ms,
            }
        )

        ctx = ToolCtx(
            session_id=session.id,
            message_id=assistant.id,
            agent=request.agent_name,
            source=request.source,
            bus=self._bus,
            store=self._store,
            perm=self._perm,
            ruleset=session.permission_rules,
            tool_part_id=call_id,
            trace_id=trace_id,
            root_session_id=request.root_session_id or session.id,
            parent_session_id=request.parent_session_id,
            parallel_group_id=parallel_group_id,
            parallel_index=parallel_index,
            parallel_size=parallel_size,
            approval_scope=contract.approval_scope,
        )
        return (
            _PreparedToolCall(
                index=index,
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                args=args,
                start_ms=start_ms,
                tool=tool,
                contract=contract,
                ctx=ctx,
            ),
            None,
        )

    async def _execute_prepared_tool_call(
        self,
        *,
        prepared: _PreparedToolCall,
        request: RunRequest,
        trace_span: Any | None,
        turn_log: Any,
    ) -> _ToolExecutionOutcome:
        tool_log = turn_log.child(tool_name=prepared.tool_name, tool_call_id=prepared.call_id)
        idempotency_key_present = bool(
            str(prepared.args.get("idempotency_key") or "").strip()
        )
        contract_metadata: dict[str, Any] = {
            "tool_contract": prepared.contract.to_metadata(),
            "idempotency_key_present": idempotency_key_present,
            "recovery_action": prepared.contract.recovery_action(
                idempotency_key_present=idempotency_key_present
            ),
        }
        tool_span_cm = nullcontext(None)
        if trace_span:
            try:
                tool_span_cm = trace_span.start_as_current_observation(
                    as_type="tool",
                    name=f"tool-{prepared.tool_name}",
                    input=prepared.args,
                    metadata={
                        "tool": prepared.tool_name,
                        "call_id": prepared.call_id,
                        "parallel_group_id": prepared.ctx.parallel_group_id,
                        "parallel_index": prepared.ctx.parallel_index,
                        "parallel_size": prepared.ctx.parallel_size,
                        "tool_contract": prepared.contract.to_metadata(),
                    },
                )
            except Exception as exc:
                tool_log.error("Error creating Langfuse tool span", event="langfuse.tool_span.start.error", exc_info=exc)

        with tool_span_cm as tool_span:
            exec_ctx = replace(
                prepared.ctx,
                parent_observation_id=getattr(tool_span, "id", None) or prepared.ctx.parent_observation_id,
            )
            try:
                result = await prepared.tool.execute(prepared.args, exec_ctx)
                if tool_span:
                    try:
                        tool_span.update(output=result.output, metadata={**result.metadata, "title": result.title})
                    except Exception as exc:
                        tool_log.error("Error updating Langfuse tool span", event="langfuse.tool_span.update.error", exc_info=exc)
                out: dict[str, object] = {"title": result.title, "output": result.output, "metadata": result.metadata}
                if self._hooks:
                    await self._hooks.trigger(
                        Hook.ToolExecuteAfter,
                        {"tool": prepared.tool_name, "session_id": prepared.ctx.session_id, "call_id": prepared.call_id},
                        out,
                    )
                title = str(out.get("title") or result.title)
                output = str(out.get("output") or result.output)
                raw_meta = out.get("metadata")
                metadata = {
                    **cast(dict[str, Any], raw_meta if isinstance(raw_meta, dict) else result.metadata),
                    **contract_metadata,
                }
                result_is_error = bool(metadata.get("error"))
                result_error_code = (
                    str(metadata.get("error_code") or "tool_reported_error")
                    if result_is_error
                    else None
                )
                end_ms = int(time.time() * 1000)
                tool_log.debug(
                    "Tool execution finished",
                    event="tool.finish",
                    duration_ms=float(end_ms - prepared.start_ms),
                    output_chars=len(output),
                )
                return _ToolExecutionOutcome(
                    call_id=prepared.call_id,
                    tool_name=prepared.tool_name,
                    part_id=prepared.part_id,
                    input=prepared.args,
                    title=title,
                    output=output,
                    metadata=metadata,
                    start_ms=prepared.start_ms,
                    end_ms=end_ms,
                    status="error" if result_is_error else "completed",
                    error_code=result_error_code,
                )
            except PermissionRejected as exc:
                if tool_span:
                    try:
                        tool_span.update(level="ERROR", metadata={"error": "PermissionRejected", "message": str(exc)})
                    except Exception as inner_exc:
                        tool_log.error(
                            "Error updating Langfuse tool span with PermissionRejected",
                            event="langfuse.tool_span.error_update.error",
                            exc_info=inner_exc,
                        )
                tool_log.warning("Tool blocked by permission", event="tool.blocked")
                end_ms = int(time.time() * 1000)
                return _ToolExecutionOutcome(
                    call_id=prepared.call_id,
                    tool_name=prepared.tool_name,
                    part_id=prepared.part_id,
                    input=prepared.args,
                    title=prepared.tool_name,
                    output=f"Permission denied: {exc}",
                    metadata={
                        "error": True,
                        "error_code": "permission_denied",
                        "retryable": False,
                        **contract_metadata,
                    },
                    start_ms=prepared.start_ms,
                    end_ms=end_ms,
                    status="error",
                    error_code="permission_denied",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if tool_span:
                    try:
                        tool_span.update(level="ERROR", metadata={"error": type(exc).__name__, "message": str(exc)})
                    except Exception as inner_exc:
                        tool_log.error(
                            "Error updating Langfuse tool span with error",
                            event="langfuse.tool_span.error_update.error",
                            exc_info=inner_exc,
                        )
                output = f"Tool execution failed: {exc}"
                out = {"title": prepared.tool_name, "output": output, "metadata": {"error": True}}
                if self._hooks:
                    await self._hooks.trigger(
                        Hook.ToolExecuteAfter,
                        {"tool": prepared.tool_name, "session_id": prepared.ctx.session_id, "call_id": prepared.call_id},
                        out,
                    )
                title = str(out.get("title") or prepared.tool_name)
                final_output = str(out.get("output") or output)
                raw_meta = out.get("metadata")
                metadata = {
                    **cast(dict[str, Any], raw_meta if isinstance(raw_meta, dict) else {"error": True}),
                    **contract_metadata,
                }
                end_ms = int(time.time() * 1000)
                tool_log.error(
                    "Tool execution failed",
                    event="tool.error",
                    duration_ms=float(end_ms - prepared.start_ms),
                    exc_info=exc,
                    output_chars=len(final_output),
                )
                return _ToolExecutionOutcome(
                    call_id=prepared.call_id,
                    tool_name=prepared.tool_name,
                    part_id=prepared.part_id,
                    input=prepared.args,
                    title=title,
                    output=final_output,
                    metadata=metadata,
                    start_ms=prepared.start_ms,
                    end_ms=end_ms,
                    status="error",
                    error_code="tool_timeout" if isinstance(exc, TimeoutError) else "tool_execution_failed",
                )

    async def _persist_tool_outcome(
        self,
        session_id: str,
        assistant: Message,
        model: ModelRef,
        outcome: _ToolExecutionOutcome,
    ) -> None:
        if self._artifact_store is not None:
            materialized = await self._artifact_store.materialize_tool_output(
                session_id=session_id,
                run_id=assistant.run_id,
                message_id=assistant.id,
                tool_call_id=outcome.call_id,
                tool_name=outcome.tool_name,
                output=outcome.output,
            )
            if materialized.artifact is not None:
                outcome = replace(
                    outcome,
                    output=materialized.output,
                    metadata={**outcome.metadata, **materialized.metadata},
                )
        operation_id = str(uuid5(NAMESPACE_URL, f"nexapilot:{assistant.run_id or session_id}:{outcome.call_id}"))
        capability, canonical_target = await self._describe_tool_operation(session_id, outcome.tool_name, outcome.input)
        executor = str(outcome.metadata.get("executor") or "builtin")
        isolation = str(outcome.metadata.get("isolation") or ("application_guard" if executor == "builtin" else "unknown"))
        outcome.metadata.setdefault("operation_id", operation_id)
        outcome.metadata.setdefault("capability", capability)
        outcome.metadata.setdefault("canonical_target", canonical_target)
        raw_contract = outcome.metadata.get("tool_contract")
        contract_side_effect = (
            str(raw_contract.get("side_effect"))
            if isinstance(raw_contract, dict)
            else "unknown"
        )
        if outcome.status == "completed":
            side_effect_state = (
                "none"
                if contract_side_effect == "none"
                else "committed"
            )
        elif outcome.error_code in {
            "permission_denied",
            "invalid_tool_arguments",
            "tool_not_found",
        }:
            side_effect_state = "none"
        else:
            side_effect_state = (
                "none"
                if contract_side_effect == "none"
                else "unknown"
            )
        outcome.metadata.setdefault("side_effect_state", side_effect_state)
        if outcome.error_code:
            outcome.metadata.setdefault("error_code", outcome.error_code)
        await self._store.upsert_tool_operation(
            {
                "operation_id": operation_id,
                "run_id": assistant.run_id,
                "session_id": session_id,
                "message_id": assistant.id,
                "tool_call_id": outcome.call_id,
                "tool_name": outcome.tool_name,
                "capability": capability,
                "canonical_target": canonical_target,
                "executor_backend": executor,
                "isolation_level": isolation,
                "status": outcome.status,
                "error_code": outcome.error_code,
                "input": outcome.input,
                "result": {"title": outcome.title, "output": outcome.output, "metadata": outcome.metadata},
                "created_at": outcome.start_ms,
                "finished_at": outcome.end_ms,
            }
        )
        state = (
            ToolStateError(
                input=outcome.input,
                error=outcome.output,
                metadata={**outcome.metadata, "error_code": outcome.error_code or "tool_execution_failed"},
                time={"start": outcome.start_ms, "end": outcome.end_ms},
            )
            if outcome.status == "error"
            else ToolStateCompleted(
                input=outcome.input,
                title=outcome.title,
                output=outcome.output,
                metadata=outcome.metadata,
                time={"start": outcome.start_ms, "end": outcome.end_ms},
            )
        )
        done = ToolPart(
            id=outcome.part_id,
            message_id=assistant.id,
            session_id=session_id,
            call_id=outcome.call_id,
            tool=outcome.tool_name,
            state=state,
        )
        await self._store.add_part(session_id, assistant.id, done)
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session_id, "message_id": assistant.id, "part": done.model_dump()},
            )
        )

        tool_msg_id = str(uuid4())
        tool_msg = Message(
            id=tool_msg_id,
            session_id=session_id,
            run_id=assistant.run_id,
            role="tool",
            parent_id=assistant.id,
            agent=assistant.agent,
            model=model,
            created_at=int(time.time() * 1000),
            tool_call_id=outcome.call_id,
            tool_name=outcome.tool_name,
        )
        tool_msg = await self._store.add_message(tool_msg)
        tool_text_part = TextPart(
            id=str(uuid4()),
            message_id=tool_msg_id,
            session_id=session_id,
            text=outcome.output,
            synthetic=True,
        )
        await self._store.add_part(session_id, tool_msg_id, tool_text_part)
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": tool_msg.model_dump()}))

    async def _describe_tool_operation(
        self, session_id: str, tool_name: str, args: dict[str, Any]
    ) -> tuple[str, str]:
        session = await self._store.get_session(session_id)
        if tool_name == "bash":
            return "process.exec.shell", str(args.get("command") or "")
        if tool_name == "write":
            raw = str(args.get("file_path") or "")
            target = Path(raw) if Path(raw).is_absolute() else Path(session.cwd) / raw
            return "fs.write", str(target.resolve())
        if tool_name in {"read", "glob", "grep", "skill"}:
            raw = str(args.get("file_path") or args.get("path") or session.cwd)
            target = Path(raw) if Path(raw).is_absolute() else Path(session.cwd) / raw
            return "fs.read", str(target.resolve())
        if tool_name in {"webfetch", "websearch"}:
            return "network.fetch", str(args.get("url") or args.get("query") or "")
        if tool_name == "task":
            return "agent.spawn", str(args.get("subagent_type") or "")
        if tool_name.startswith("memory_"):
            return "memory.read", str(args.get("query") or args.get("id") or "")
        if tool_name == "todowrite":
            return "session.state.write", session_id
        return "mcp.call", tool_name

    def _immediate_tool_outcome(
        self,
        *,
        call_id: str,
        tool_name: str,
        part_id: str,
        input: dict[str, Any],
        output: str,
        title: str | None = None,
        start_ms: int | None = None,
        error_code: str = "tool_execution_failed",
    ) -> _ToolExecutionOutcome:
        now = int(time.time() * 1000)
        return _ToolExecutionOutcome(
            call_id=call_id,
            tool_name=tool_name,
            part_id=part_id,
            input=input,
            title=title or tool_name,
            output=output,
            metadata={"error": True, "error_code": error_code, "retryable": False},
            start_ms=start_ms or now,
            end_ms=now,
            status="error",
            error_code=error_code,
        )

    def _is_parallel_task_batch(self, calls: list[ToolCall]) -> bool:
        return 2 <= len(calls) <= 3 and all(call.name == "task" for call in calls)

    def _to_openai_messages(
        self,
        history: list[MessageWithParts],
        *,
        provider_state_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Responses reasoning items are opaque, ordered protocol state.  A Run
        # currently persists all of its model rounds on one assistant Message,
        # while tool outputs are separate Messages.  Replaying that assistant
        # as one aggregate would move every reasoning item before every
        # function call and make encrypted reasoning state unverifiable.
        # Index tool outputs up front so an assistant Message can be projected
        # back into its original per-round sequence.
        tool_outputs = {
            message.info.tool_call_id: {
                "role": "tool",
                "tool_call_id": message.info.tool_call_id,
                "content": "".join(
                    part.text
                    for part in message.parts
                    if getattr(part, "type", "") == "text"
                ),
            }
            for message in history
            if message.info.role == "tool" and message.info.tool_call_id
        }
        consumed_tool_outputs: set[str] = set()
        out: list[dict[str, Any]] = []
        for message in history:
            if message.info.role == "user":
                txt = "".join([part.text for part in message.parts if getattr(part, "type", "") == "text"])
                out.append({"role": "user", "content": txt})
                continue
            if message.info.role == "assistant":
                txt = "".join([part.text for part in message.parts if getattr(part, "type", "") == "text"])
                reasoning = "".join([part.text for part in message.parts if getattr(part, "type", "") == "reasoning"])
                provider_state = [
                    part.data
                    for part in message.parts
                    if getattr(part, "type", "") == "provider_state"
                    and getattr(part, "provider", "") == "openai_responses"
                    and not bool(getattr(part, "data", {}).get("_nexa_rejected"))
                    and (
                        provider_state_run_id is None
                        or message.info.run_id == provider_state_run_id
                    )
                ]
                if provider_state:
                    self._append_responses_rounds(
                        out=out,
                        message=message,
                        tool_outputs=tool_outputs,
                        consumed_tool_outputs=consumed_tool_outputs,
                    )
                    continue
                calls: dict[str, dict[str, Any]] = {}
                for part in message.parts:
                    if getattr(part, "type", "") != "tool":
                        continue
                    state = getattr(part, "state", None)
                    args_json = ""
                    if state and getattr(state, "status", "") == "pending":
                        args_json = str(getattr(state, "raw", "") or "")
                    if not args_json and state and getattr(state, "input", None) is not None:
                        args_json = json.dumps(getattr(state, "input"))
                    calls[part.call_id] = {
                        "id": part.call_id,
                        "type": "function",
                        "function": {"name": part.tool, "arguments": args_json},
                    }
                msg: dict[str, Any] = {"role": "assistant", "content": txt or ""}
                if reasoning:
                    msg["reasoning_content"] = reasoning
                if provider_state:
                    msg["provider_state"] = provider_state
                if calls:
                    msg["tool_calls"] = list(calls.values())
                out.append(msg)
                continue
            if message.info.role == "tool":
                if message.info.tool_call_id in consumed_tool_outputs:
                    continue
                txt = "".join([part.text for part in message.parts if getattr(part, "type", "") == "text"])
                out.append({"role": "tool", "tool_call_id": message.info.tool_call_id, "content": txt})
        return out

    @staticmethod
    def _append_responses_rounds(
        *,
        out: list[dict[str, Any]],
        message: MessageWithParts,
        tool_outputs: dict[str, dict[str, Any]],
        consumed_tool_outputs: set[str],
    ) -> None:
        """Project one persisted assistant Message into ordered Responses rounds."""
        state_items: list[dict[str, Any]] = []
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[str, dict[str, Any]] = {}

        def flush() -> None:
            if not state_items and not text_parts and not reasoning_parts and not calls:
                return
            projected: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts),
            }
            if reasoning_parts:
                projected["reasoning_content"] = "".join(reasoning_parts)
            if state_items:
                projected["provider_state"] = list(state_items)
            if calls:
                projected["tool_calls"] = list(calls.values())
            out.append(projected)
            for call_id in calls:
                output = tool_outputs.get(call_id)
                if output is not None:
                    out.append(output)
                    consumed_tool_outputs.add(call_id)
            state_items.clear()
            text_parts.clear()
            reasoning_parts.clear()
            calls.clear()

        for part in message.parts:
            part_type = getattr(part, "type", "")
            if (
                part_type == "provider_state"
                and getattr(part, "provider", "") == "openai_responses"
                and not bool(
                    getattr(part, "data", {}).get("_nexa_rejected")
                )
            ):
                # A reasoning output item marks the start of a new provider
                # response.  Close the preceding call/output group before it.
                if calls or text_parts:
                    flush()
                state_items.append(part.data)
                continue
            if part_type == "reasoning":
                reasoning_parts.append(part.text)
                continue
            if part_type == "text":
                text_parts.append(part.text)
                continue
            if part_type != "tool":
                continue
            state = getattr(part, "state", None)
            args_json = ""
            if state and getattr(state, "status", "") == "pending":
                args_json = str(getattr(state, "raw", "") or "")
            if not args_json and state and getattr(state, "input", None) is not None:
                args_json = json.dumps(getattr(state, "input"))
            calls[part.call_id] = {
                "id": part.call_id,
                "type": "function",
                "function": {"name": part.tool, "arguments": args_json},
            }
        flush()

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from nexapilot.agents.registry import AgentRegistry, agent_registry
from nexapilot.agents.types import AgentDefinition, RunRequest, RunResult
from nexapilot.agents.workspaces import GitWorktreeManager
from nexapilot.artifacts import ArtifactStore
from nexapilot.bus.bus import Bus, Event
from nexapilot.config import Config
from nexapilot.hookdefs import Hook
from nexapilot.hooks import Hooker
from nexapilot.llm.protocol import LLMProvider
from nexapilot.log import logger
from nexapilot.loop.interrupt import InterruptManager
from nexapilot.loop.session_loop import SessionLoop
from nexapilot.memory.service import MemoryService
from nexapilot.model import (
    Message,
    ModelRef,
    PermissionRule,
    Session,
    SessionRuntime,
    TextPart,
)
from nexapilot.permission.service import PermissionService
from nexapilot.runtime import DaytonaManager
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.task_runtime import TaskRuntimeService
from nexapilot.tools.base import ToolResult
from nexapilot.tools.registry import ToolRegistry


class _BatchTaskContext:
    def __init__(
        self,
        *,
        parent: Any,
        parallel_group_id: str,
        parallel_index: int,
        parallel_size: int,
    ) -> None:
        self.tool_part_id = parent.tool_part_id
        self.trace_id = getattr(parent, "trace_id", None)
        self.parent_observation_id = getattr(
            parent, "parent_observation_id", None
        )
        self.parallel_group_id = parallel_group_id
        self.parallel_index = parallel_index
        self.parallel_size = parallel_size


class AgentService:
    def __init__(
        self,
        *,
        cfg: Config,
        bus: Bus,
        store: SQLiteStore,
        perm: PermissionService,
        llm: LLMProvider,
        interrupt: InterruptManager,
        hooks: Hooker | None,
        memory_service: MemoryService,
        daytona_manager: DaytonaManager,
        mcp_manager: Any,
        task_runtime: TaskRuntimeService | None = None,
        kb_client: Any = None,
        registry: AgentRegistry | None = None,
        workspace_manager: GitWorktreeManager | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._perm = perm
        self._llm = llm
        self._interrupt = interrupt
        self._hooks = hooks
        self._memory_service = memory_service
        self._daytona_manager = daytona_manager
        self._mcp_manager = mcp_manager
        self._task_runtime = task_runtime or TaskRuntimeService(cfg.db_path, bus)
        self._kb_client = kb_client
        self._registry = registry or agent_registry
        self._workspace_manager = workspace_manager or GitWorktreeManager(
            store=store, bus=bus
        )
        self._artifact_store = artifact_store
        self._log = logger.child(service="agent.service")
        self._active_runs: set[str] = set()
        self._active_runs_lock = asyncio.Lock()
        self._owner_id = f"agent-worker:{uuid4()}"
        self._agent_slots = {
            agent.name: asyncio.Semaphore(agent.limits.max_concurrency)
            for agent in self._registry.list(mode="subagent")
            if agent.limits.max_concurrency is not None
        }

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def workspace_manager(self) -> GitWorktreeManager:
        return self._workspace_manager

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def get_agent(self, name: str) -> AgentDefinition:
        return self._registry.get(name)

    async def add_user_message(self, session: Session, text: str, *, source: str = "api") -> str:
        if not text.strip():
            raise ValueError("text is required")
        now = int(time.time() * 1000)
        msg = Message(
            id=str(uuid4()),
            session_id=session.id,
            role="user",
            parent_id=None,
            agent=session.agent_name,
            model=ModelRef(provider="openai-compatible", id=self._cfg.openai.model),
            created_at=now,
        )
        out: dict[str, object] = {"text": text}
        if self._hooks:
            await self._hooks.trigger(Hook.ChatMessage, {"session_id": session.id, "agent": msg.agent, "message_id": msg.id}, out)
        text = str(out.get("text") or text)
        await self._store.add_message(msg)
        text_part = TextPart(id=str(uuid4()), message_id=msg.id, session_id=session.id, text=text)
        await self._store.add_part(session.id, msg.id, text_part)
        await self._store.touch_session(session.id)
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session.id, "info": msg.model_dump()}))
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session.id, "message_id": msg.id, "part": text_part.model_dump(), "delta": text},
            ),
        )
        self._log.info(
            "User message stored",
            event="message.user.added",
            session_id=session.id,
            message_id=msg.id,
            source=source,
            agent=session.agent_name,
            content_chars=len(text),
        )
        return msg.id

    async def create_child_session(
        self,
        *,
        parent_session: Session,
        agent_name: str,
        description: str,
        parent_tool_call_id: str,
    ) -> Session:
        now = int(time.time() * 1000)
        child_session_id = str(uuid4())
        agent = self.get_agent(agent_name)
        workspace = await self._workspace_manager.provision(
            parent_session=parent_session,
            child_session_id=child_session_id,
            policy=agent.workspace,
        )
        execution_worktree = (
            str(workspace["worktree_path"])
            if workspace is not None
            else parent_session.worktree
        )
        execution_cwd = (
            execution_worktree if workspace is not None else parent_session.cwd
        )
        session = Session(
            id=child_session_id,
            title=f"[{agent_name}] {description}",
            worktree=execution_worktree,
            cwd=execution_cwd,
            created_at=now,
            updated_at=now,
            permission_rules=self._build_child_permission_rules(parent_session.permission_rules, agent_name),
            runtime=SessionRuntime.model_validate(parent_session.runtime.model_dump()),
            kind="subagent",
            agent_name=agent_name,
            root_session_id=parent_session.root_session_id or parent_session.id,
            parent_session_id=parent_session.id,
            parent_tool_call_id=parent_tool_call_id,
            project_worktree=parent_session.memory_worktree,
            project_id=parent_session.project_id,
        )
        try:
            await self._store.create_session(session)
        except Exception:
            if workspace is not None:
                await self._workspace_manager.release(workspace["id"], force=True)
            raise
        if session.runtime.backend == "local":
            await self._memory_service.ensure_worktree(session.memory_worktree)
        await self._bus.publish(Event(type="session.created", properties={"session_id": session.id, "info": session.model_dump()}))
        return session

    async def resolve_child_session(self, *, parent_session: Session, session_id: str, agent_name: str) -> Session:
        session = await self._store.get_session(session_id)
        root_session_id = parent_session.root_session_id or parent_session.id
        if session.kind != "subagent":
            raise ValueError(f"session is not a subagent session: {session_id}")
        if session.agent_name != agent_name:
            raise ValueError(f"subagent session agent mismatch: expected {agent_name}, got {session.agent_name}")
        if (session.root_session_id or session.id) != root_session_id:
            raise ValueError("subagent session belongs to a different root session")
        if await self.is_session_busy(session.id):
            raise ValueError(f"subagent session is currently busy: {session.id}")
        agent = self.get_agent(agent_name)
        try:
            await self._store.get_agent_workspace_for_session(session.id)
            has_isolated_workspace = True
        except KeyError:
            has_isolated_workspace = False
        if agent.workspace.mode == "git_worktree" and not has_isolated_workspace:
            raise ValueError("subagent session is missing its isolated workspace")
        if has_isolated_workspace:
            await self._workspace_manager.ensure_resumable(session.id)
        return session

    async def extract_assistant_text(self, session_id: str, message_id: str) -> str:
        history = await self._store.list_messages(session_id)
        target = next((m for m in history if m.info.id == message_id), None)
        if not target:
            return ""
        chunks = [getattr(part, "text", "") for part in target.parts if getattr(part, "type", None) == "text"]
        return "".join(chunks).strip()

    async def run(self, request: RunRequest, *, tools: ToolRegistry | None = None) -> RunResult:
        await self._mark_session_busy(request.session_id)
        try:
            session = await self._store.get_session(request.session_id)
            agent = self.get_agent(request.agent_name)
            tool_registry = tools or await self.build_tools(session, request.agent_name)
            loop = SessionLoop(
                cfg=self._cfg,
                bus=self._bus,
                store=self._store,
                perm=self._perm,
                tools=tool_registry,
                llm=self._llm,
                interrupt=self._interrupt,
                hooks=self._hooks,
                agent_definition=agent,
                context_manager=self._memory_service.context_manager,
                artifact_store=self._artifact_store,
                owner_id=self._owner_id,
            )
            assistant_message_id, trace_id, finish = await loop.run(request=request)
            return RunResult(assistant_message_id=assistant_message_id, trace_id=trace_id, finish=finish)
        finally:
            await self._mark_session_idle(request.session_id)

    async def is_session_busy(self, session_id: str) -> bool:
        async with self._active_runs_lock:
            return session_id in self._active_runs

    async def release_workspace(
        self, workspace_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        record = await self._store.get_agent_workspace(workspace_id)
        if await self.is_session_busy(record["child_session_id"]):
            raise RuntimeError("cannot release a workspace while its session is busy")
        return await self._workspace_manager.release(workspace_id, force=force)

    async def _mark_session_busy(self, session_id: str) -> None:
        async with self._active_runs_lock:
            if session_id in self._active_runs:
                raise RuntimeError(f"session is already running: {session_id}")
            self._active_runs.add(session_id)

    async def _mark_session_idle(self, session_id: str) -> None:
        async with self._active_runs_lock:
            self._active_runs.discard(session_id)

    async def execute_task(
        self,
        *,
        parent_session: Session,
        description: str,
        prompt: str,
        subagent_type: str,
        resume_session_id: str | None,
        parent_ctx: Any,
        plan_task_id: str | None = None,
    ) -> ToolResult:
        agent = self.get_agent(subagent_type)
        if agent.mode != "subagent":
            raise ValueError(f"agent is not a subagent: {subagent_type}")
        if plan_task_id:
            linked = await self._task_runtime.get_task(plan_task_id)
            task = linked["task"]
            if linked["session_id"] != parent_session.id:
                raise ValueError("plan_task_id belongs to a different session")
            if task.status != "ready":
                raise ValueError(
                    f"plan task must be ready before delegation: {task.status}"
                )
            if task.execution_mode != "agent":
                raise ValueError("plan task is assigned to a human")
        await parent_ctx.ask(
            permission="task",
            patterns=[subagent_type],
            always=[subagent_type],
            metadata={
                "description": description,
                "subagent_type": subagent_type,
                "plan_task_id": plan_task_id,
            },
        )
        slot = self._agent_slots.get(subagent_type)
        if slot is None:
            return await self._execute_task(
                parent_session=parent_session,
                description=description,
                prompt=prompt,
                subagent_type=subagent_type,
                resume_session_id=resume_session_id,
                plan_task_id=plan_task_id,
                parent_ctx=parent_ctx,
            )
        async with slot:
            return await self._execute_task(
                parent_session=parent_session,
                description=description,
                prompt=prompt,
                subagent_type=subagent_type,
                resume_session_id=resume_session_id,
                plan_task_id=plan_task_id,
                parent_ctx=parent_ctx,
            )

    async def execute_task_batch(
        self,
        *,
        parent_session: Session,
        tasks: list[dict[str, Any]],
        parent_ctx: Any,
    ) -> ToolResult:
        if not 2 <= len(tasks) <= 8:
            raise ValueError("taskbatch requires between 2 and 8 tasks")

        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(tasks):
            description = str(raw.get("description", "")).strip()
            prompt = str(raw.get("prompt", "")).strip()
            subagent_type = str(raw.get("subagent_type", "")).strip()
            resume_session_id = str(raw.get("session_id", "")).strip() or None
            plan_task_id = str(raw.get("plan_task_id", "")).strip() or None
            if not description or not prompt or not subagent_type:
                raise ValueError(
                    f"task {index} requires description, prompt, and subagent_type"
                )
            agent = self.get_agent(subagent_type)
            if agent.mode != "subagent":
                raise ValueError(f"agent is not a subagent: {subagent_type}")
            if plan_task_id:
                linked = await self._task_runtime.get_task(plan_task_id)
                plan_task = linked["task"]
                if linked["session_id"] != parent_session.id:
                    raise ValueError("plan_task_id belongs to a different session")
                if plan_task.status != "ready":
                    raise ValueError(
                        "plan task must be ready before delegation: "
                        f"{plan_task.status}"
                    )
                if plan_task.execution_mode != "agent":
                    raise ValueError("plan task is assigned to a human")
            normalized.append(
                {
                    "description": description,
                    "prompt": prompt,
                    "subagent_type": subagent_type,
                    "resume_session_id": resume_session_id,
                    "plan_task_id": plan_task_id,
                }
            )

        subagent_types = sorted({item["subagent_type"] for item in normalized})
        await parent_ctx.ask(
            permission="task",
            patterns=subagent_types,
            always=subagent_types,
            metadata={
                "description": f"Parallel batch of {len(normalized)} subagent tasks",
                "subagent_types": subagent_types,
                "task_count": len(normalized),
            },
        )

        parallel_group_id = f"taskbatch:{parent_ctx.tool_part_id}"

        async def execute_one(index: int, item: dict[str, Any]) -> ToolResult:
            task_ctx = _BatchTaskContext(
                parent=parent_ctx,
                parallel_group_id=parallel_group_id,
                parallel_index=index,
                parallel_size=len(normalized),
            )
            slot = self._agent_slots.get(item["subagent_type"])
            if slot is None:
                return await self._execute_task(
                    parent_session=parent_session,
                    parent_ctx=task_ctx,
                    **item,
                )
            async with slot:
                return await self._execute_task(
                    parent_session=parent_session,
                    parent_ctx=task_ctx,
                    **item,
                )

        gathered = await asyncio.gather(
            *(execute_one(index, item) for index, item in enumerate(normalized)),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        outputs: list[str] = []
        failed = False
        for index, result in enumerate(gathered):
            if isinstance(result, BaseException):
                failed = True
                detail = {
                    "index": index,
                    "description": normalized[index]["description"],
                    "status": "error",
                    "error_code": "batch_task_failed",
                    "error": str(result)[:500],
                }
                results.append(detail)
                outputs.append(
                    f"## {normalized[index]['description']}\n"
                    f"Subagent delegation failed: {detail['error']}"
                )
                continue
            metadata = dict(result.metadata)
            status = str(metadata.get("status") or "completed")
            if status != "completed":
                failed = True
            results.append(
                {
                    "index": index,
                    "description": normalized[index]["description"],
                    "status": status,
                    **metadata,
                }
            )
            outputs.append(f"## {result.title}\n{result.output}")

        metadata: dict[str, Any] = {
            "parallel_group_id": parallel_group_id,
            "task_count": len(normalized),
            "completed_count": sum(
                1 for item in results if item.get("status") == "completed"
            ),
            "results": results,
        }
        if failed:
            metadata.update(
                {
                    "error": True,
                    "error_code": "taskbatch_partial_failure",
                    "retryable": False,
                }
            )
        return ToolResult(
            title=f"Parallel subagent batch ({len(normalized)} tasks)",
            output="\n\n".join(outputs),
            metadata=metadata,
        )

    async def _execute_task(
        self,
        *,
        parent_session: Session,
        description: str,
        prompt: str,
        subagent_type: str,
        resume_session_id: str | None,
        plan_task_id: str | None,
        parent_ctx: Any,
    ) -> ToolResult:
        agent = self.get_agent(subagent_type)

        if resume_session_id:
            child_session = await self.resolve_child_session(
                parent_session=parent_session,
                session_id=resume_session_id,
                agent_name=subagent_type,
            )
        else:
            child_session = await self.create_child_session(
                parent_session=parent_session,
                agent_name=subagent_type,
                description=description,
                parent_tool_call_id=parent_ctx.tool_part_id,
            )

        plan_actor = f"agent:{parent_session.agent_name}"
        if plan_task_id:
            linked = await self._task_runtime.get_task(plan_task_id)
            task = linked["task"]
            await self._task_runtime.transition_task(
                plan_task_id,
                target="running",
                expected_revision=task.revision,
                actor=plan_actor,
                reason="delegated_to_subagent",
            )

        await self._bus.publish(
            Event(
                type="task.started",
                properties={
                    "session_id": parent_session.id,
                    "child_session_id": child_session.id,
                    "subagent_type": subagent_type,
                    "call_id": parent_ctx.tool_part_id,
                    "description": description,
                    "plan_task_id": plan_task_id,
                    "parallel_group_id": getattr(parent_ctx, "parallel_group_id", None),
                    "parallel_index": getattr(parent_ctx, "parallel_index", None),
                    "parallel_size": getattr(parent_ctx, "parallel_size", None),
                },
            ),
        )

        await self.add_user_message(child_session, prompt, source=f"task:{parent_ctx.tool_part_id}")
        request = RunRequest(
            session_id=child_session.id,
            agent_name=subagent_type,
            source=f"task:{parent_ctx.tool_part_id}",
            root_session_id=child_session.root_session_id or child_session.id,
            parent_session_id=parent_session.id,
            parent_tool_call_id=parent_ctx.tool_part_id,
            trace_id=getattr(parent_ctx, "trace_id", None),
            parent_observation_id=getattr(parent_ctx, "parent_observation_id", None),
            limits=agent.limits,
        )

        started = time.monotonic()
        child_task = asyncio.create_task(self.run(request))
        timed_out = False
        interrupted = False
        while not child_task.done():
            done, _pending = await asyncio.wait({child_task}, timeout=0.25)
            if done:
                break
            if await self._interrupt.is_interrupted(parent_session.id):
                interrupted = True
                await self._interrupt.interrupt(child_session.id, reason="parent_cancelled")
                child_task.cancel()
                break
            max_wall_time_ms = agent.limits.max_wall_time_ms or 0
            if max_wall_time_ms > 0 and (time.monotonic() - started) * 1000 >= max_wall_time_ms:
                timed_out = True
                await self._interrupt.interrupt(child_session.id, reason="timed_out")
                child_task.cancel()
                break

        result: RunResult | None = None
        error_code: str | None = None
        error_message: str | None = None
        try:
            result = await child_task
        except asyncio.CancelledError:
            error_code = "timed_out" if timed_out else "interrupted"
        except Exception as exc:
            error_code = "run_failed"
            error_message = str(exc)

        status = "completed"
        assistant_message_id: str | None = None
        trace_id: str | None = None
        if result is not None:
            assistant_message_id = result.assistant_message_id
            trace_id = result.trace_id
            failure_finishes = {
                "error",
                "max_turns_exceeded",
                "max_tool_calls_exceeded",
                "blocked",
                "provider_stream_interrupted",
            }
            interrupted_finishes = {"interrupted", "user_cancelled", "run_lease_lost"}
            if result.finish in failure_finishes:
                status = result.finish
            elif result.finish in interrupted_finishes:
                status = "interrupted"
        elif timed_out:
            status = "timed_out"
        elif interrupted:
            status = "interrupted"
        else:
            status = "error"

        text = ""
        if assistant_message_id:
            text = await self.extract_assistant_text(child_session.id, assistant_message_id)
        if not text:
            if status == "timed_out":
                text = "Subagent timed out before producing a final summary."
            elif status == "interrupted":
                text = "Subagent was interrupted before producing a final summary."
            elif error_message:
                text = f"Subagent failed: {error_message}"
            else:
                text = "Subagent returned no summary."

        child_runs = await self._store.list_runs(child_session.id, limit=1)
        child_run_id = child_runs[0].id if child_runs else None

        plan_sync_error: str | None = None
        if plan_task_id:
            try:
                linked = await self._task_runtime.get_task(plan_task_id)
                task = linked["task"]
                if status == "completed":
                    await self._task_runtime.transition_task(
                        plan_task_id,
                        target="completed",
                        expected_revision=task.revision,
                        actor=plan_actor,
                        reason="subagent_completed",
                        run_id=child_run_id,
                        result_payload={
                            "child_session_id": child_session.id,
                            "assistant_message_id": assistant_message_id,
                            "finish_reason": result.finish if result else None,
                            "summary": text[:4000],
                        },
                    )
                else:
                    await self._task_runtime.transition_task(
                        plan_task_id,
                        target="failed",
                        expected_revision=task.revision,
                        actor=plan_actor,
                        reason="subagent_failed",
                        run_id=child_run_id,
                        error_payload={
                            "child_session_id": child_session.id,
                            "finish_reason": result.finish if result else status,
                            "error_code": error_code or status,
                            "error_message": (error_message or text)[:1000],
                        },
                    )
            except Exception as exc:
                plan_sync_error = str(exc)
                self._log.error(
                    "Failed to synchronize delegated Plan Task",
                    event="task.delegate.plan_sync.error",
                    session_id=parent_session.id,
                    child_session_id=child_session.id,
                    plan_task_id=plan_task_id,
                    error=plan_sync_error,
                )

        metadata: dict[str, Any] = {
            "session_id": child_session.id,
            "subagent_type": subagent_type,
            "assistant_message_id": assistant_message_id,
            "trace_id": trace_id,
            "status": status,
            "finish_reason": result.finish if result is not None else None,
            "plan_task_id": plan_task_id,
            "child_run_id": child_run_id,
        }
        if error_code:
            metadata["error_code"] = error_code
        if error_message:
            metadata["error_message"] = error_message[:500]
        if plan_sync_error:
            metadata["plan_sync_error"] = plan_sync_error[:500]
        if status != "completed":
            metadata.update(
                {
                    "error": True,
                    "error_code": error_code or status,
                    "retryable": status not in {"interrupted"},
                }
            )
        if getattr(parent_ctx, "parallel_group_id", None):
            metadata["parallel_group_id"] = parent_ctx.parallel_group_id

        workspace = await self._workspace_manager.retain(child_session.id)
        if workspace is not None:
            metadata.update(
                {
                    "workspace_id": workspace["id"],
                    "workspace_mode": "git_worktree",
                    "workspace_path": workspace["worktree_path"],
                    "branch_name": workspace["branch_name"],
                    "base_commit": workspace["base_commit"],
                    "head_commit": workspace["head_commit"],
                    "dirty": workspace["dirty"],
                    "workspace_status": workspace["status"],
                }
            )

        output = self._format_task_output(text=text, metadata=metadata)
        event_type = "task.finished" if status == "completed" else "task.failed"
        await self._bus.publish(
            Event(
                type=event_type,
                properties={
                    "session_id": parent_session.id,
                    "child_session_id": child_session.id,
                    "subagent_type": subagent_type,
                    "call_id": parent_ctx.tool_part_id,
                    "description": description,
                    "plan_task_id": plan_task_id,
                    "child_run_id": child_run_id,
                    "trace_id": trace_id,
                    "status": status,
                    "parallel_group_id": getattr(parent_ctx, "parallel_group_id", None),
                    "parallel_index": getattr(parent_ctx, "parallel_index", None),
                    "parallel_size": getattr(parent_ctx, "parallel_size", None),
                },
            ),
        )
        self._log.info(
            "Task finished",
            event=(
                "task.delegate.finish"
                if status == "completed"
                else "task.delegate.timeout"
                if status == "timed_out"
                else "task.delegate.interrupted"
                if status == "interrupted"
                else "task.delegate.error"
            ),
            session_id=parent_session.id,
            parent_session_id=parent_session.id,
            root_session_id=parent_session.root_session_id or parent_session.id,
            tool_call_id=parent_ctx.tool_part_id,
            agent=subagent_type,
            child_session_id=child_session.id,
            trace_id=trace_id,
            status=status,
            parallel_group_id=getattr(parent_ctx, "parallel_group_id", None),
            parallel_index=getattr(parent_ctx, "parallel_index", None),
            parallel_size=getattr(parent_ctx, "parallel_size", None),
        )
        return ToolResult(title=description, output=output, metadata=metadata)

    async def build_tools(self, session: Session, agent_name: str) -> ToolRegistry:
        from nexapilot.mcp import MCPToolAdapter
        from nexapilot.tools.bash import BashCtx, BashTool
        from nexapilot.tools.daytona import (
            DaytonaBashTool,
            DaytonaCtx,
            DaytonaGlobTool,
            DaytonaGrepTool,
            DaytonaReadTool,
            DaytonaWriteTool,
        )
        from nexapilot.tools.files import FileCtx, ReadTool, WriteTool
        from nexapilot.tools.grep import GlobTool, GrepTool, SearchCtx
        from nexapilot.tools.kb_search import KBSearchCtx, KBSearchTool
        from nexapilot.tools.memory import (
            MemoryGetTool,
            MemorySearchTool,
            MemoryToolCtx,
        )
        from nexapilot.tools.skill import SkillCtx, SkillTool
        from nexapilot.tools.task import TaskTool
        from nexapilot.tools.taskbatch import TaskBatchTool
        from nexapilot.tools.taskplan import TaskPlanTool
        from nexapilot.tools.todo import TodoWriteTool
        from nexapilot.tools.web import TavilySearchTool, WebFetchTool, WebSearchCtx

        agent = self.get_agent(agent_name)
        runtime_tools: list[Any]
        if session.runtime.backend == "daytona":
            sandbox_ref = await self._daytona_manager.get_sandbox_for_session(session)
            daytona_ctx = DaytonaCtx(
                worktree=session.worktree,
                cwd=session.cwd,
                sandbox=sandbox_ref.sandbox,
                sandbox_id=sandbox_ref.sandbox_id,
                manager=self._daytona_manager,
            )
            runtime_tools = [
                DaytonaBashTool(daytona_ctx),
                DaytonaReadTool(daytona_ctx),
                DaytonaGlobTool(daytona_ctx),
                DaytonaGrepTool(daytona_ctx),
                DaytonaWriteTool(daytona_ctx),
            ]
        else:
            runtime_tools = [
                BashTool(
                    BashCtx(
                        worktree=session.worktree,
                        cwd=session.cwd,
                        enabled=self._cfg.local_guarded.enabled,
                        require_isolated_shell=self._cfg.local_guarded.require_isolated_shell,
                        default_timeout_ms=self._cfg.local_guarded.timeout_ms,
                        max_timeout_ms=self._cfg.local_guarded.max_timeout_ms,
                        max_output_bytes=self._cfg.local_guarded.max_output_bytes,
                    )
                ),
                ReadTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
                GlobTool(SearchCtx(worktree=session.worktree, cwd=session.cwd)),
                GrepTool(SearchCtx(worktree=session.worktree, cwd=session.cwd)),
                WriteTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
            ]

        builtin_tools = runtime_tools + [
            TavilySearchTool(WebSearchCtx(tavily_api_key=self._cfg.web_search.tavily_api_key)),
            WebFetchTool(),
            SkillTool(SkillCtx(worktree=session.worktree, cwd=session.cwd, permission_rules=session.permission_rules)),
            TodoWriteTool(store=self._store, bus=self._bus),
        ]

        if agent.mode == "primary":
            builtin_tools.append(TaskPlanTool(service=self._task_runtime))
            builtin_tools.append(TaskBatchTool(service=self, parent_session=session))
            builtin_tools.append(TaskTool(service=self, parent_session=session))

        if self._kb_client is not None:
            builtin_tools.append(KBSearchTool(KBSearchCtx(kb=self._kb_client)))

        if session.runtime.backend == "local" and self._memory_service.enabled:
            manager = await self._memory_service.get_manager(
                session.memory_worktree
            )
            if manager is not None:
                memory_ctx = MemoryToolCtx(manager=manager)
                builtin_tools.extend([MemorySearchTool(memory_ctx), MemoryGetTool(memory_ctx)])

        mcp_tool_infos = await self._mcp_manager.list_tools()
        builtin_tools.extend([MCPToolAdapter(info, self._mcp_manager) for info in mcp_tool_infos])

        if agent.tool_allowlist is not None:
            allow = set(agent.tool_allowlist)
            builtin_tools = [tool for tool in builtin_tools if getattr(tool, "name", "") in allow]
        return ToolRegistry(builtin_tools)

    def _build_child_permission_rules(
        self, parent_rules: list[PermissionRule], agent_name: str
    ) -> list[PermissionRule]:
        agent = self.get_agent(agent_name)
        relevant = set(agent.tool_allowlist or ()) | {
            "external_directory",
            "task",
            "*",
        }
        inherited_denies = [
            rule for rule in parent_rules if rule.action == "deny" and rule.permission in relevant
        ]
        return [*inherited_denies, *agent.permission_profile]

    def _format_task_output(self, *, text: str, metadata: dict[str, Any]) -> str:
        lines = [
            "<subagent_summary>",
            text.strip(),
            "</subagent_summary>",
            "",
            "<task_metadata>",
        ]
        for key in (
            "session_id",
            "subagent_type",
            "assistant_message_id",
            "trace_id",
            "status",
            "error_code",
            "error_message",
            "parallel_group_id",
            "workspace_id",
            "workspace_mode",
            "workspace_path",
            "branch_name",
            "base_commit",
            "head_commit",
            "dirty",
            "workspace_status",
        ):
            value = metadata.get(key)
            if value in (None, ""):
                continue
            lines.append(f"{key}: {value}")
        lines.append("</task_metadata>")
        return "\n".join(lines)

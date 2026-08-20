from __future__ import annotations

from html import escape
from typing import Any

from nexapilot.tools.base import (
    ApprovalScope, Compensation, Idempotency, RetryPolicy, SideEffect,
    ToolContract, ToolResult,
)


class TaskTool:
    name = "task"
    contract = ToolContract(
        SideEffect.LOCAL_WRITE, Idempotency.UNSAFE, RetryPolicy.NEVER,
        Compensation.MANUAL, ApprovalScope.SESSION,
    )

    def __init__(self, *, service, parent_session) -> None:
        self._service = service
        self._parent_session = parent_session
        self.description = self._build_description()

    def schema(self) -> dict[str, Any]:
        subagent_names = [
            agent.name for agent in self._service.registry.list(mode="subagent")
        ]
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short task description for progress display.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions for the subagent.",
                },
                "subagent_type": {"type": "string", "enum": subagent_names},
                "session_id": {
                    "type": "string",
                    "description": "Optional existing subagent session to continue.",
                },
                "plan_task_id": {
                    "type": "string",
                    "description": (
                        "Optional durable Plan Task ID returned by taskplan. When "
                        "provided, the task must be ready and its state is updated "
                        "to running, then completed or failed from the child Run."
                    ),
                },
            },
            "required": ["description", "prompt", "subagent_type"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        description = str(args.get("description", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        subagent_type = str(args.get("subagent_type", "")).strip()
        session_id = str(args.get("session_id", "")).strip() or None
        plan_task_id = str(args.get("plan_task_id", "")).strip() or None
        if not description:
            raise ValueError("description is required")
        if not prompt:
            raise ValueError("prompt is required")
        if not subagent_type:
            raise ValueError("subagent_type is required")
        return await self._service.execute_task(
            parent_session=self._parent_session,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            resume_session_id=session_id,
            plan_task_id=plan_task_id,
            parent_ctx=ctx,
        )

    def _build_description(self) -> str:
        agents = self._service.registry.list(mode="subagent")
        lines = [
            "Launch a subagent for a focused subtask.",
            "When delegating a durable taskplan task, always pass its exact "
            "plan_task_id so child success or failure updates the source of truth.",
            "To run subagents in parallel, emit a single assistant response "
            "containing only multiple task calls.",
            "",
            "Available subagent types:",
        ]
        for agent in agents:
            capabilities = ", ".join(sorted(agent.capabilities))
            suffix = f" [capabilities: {escape(capabilities)}]" if capabilities else ""
            lines.append(
                f"- {escape(agent.name)}: {escape(agent.description)}{suffix}"
            )
        return "\n".join(lines)

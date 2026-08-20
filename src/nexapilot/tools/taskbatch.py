from __future__ import annotations

from html import escape
from typing import Any

from nexapilot.tools.base import (
    ApprovalScope,
    Compensation,
    Idempotency,
    RetryPolicy,
    SideEffect,
    ToolContract,
    ToolResult,
)


class TaskBatchTool:
    """Deterministic fan-out for independent Subagent work."""

    name = "taskbatch"
    contract = ToolContract(
        SideEffect.LOCAL_WRITE,
        Idempotency.UNSAFE,
        RetryPolicy.NEVER,
        Compensation.MANUAL,
        ApprovalScope.SESSION,
    )

    def __init__(self, *, service, parent_session) -> None:
        self._service = service
        self._parent_session = parent_session
        names = [agent.name for agent in service.registry.list(mode="subagent")]
        self.description = (
            "Launch 2-8 independent subagent tasks concurrently after one approval. "
            "Use this instead of sequential task calls when work can run in parallel. "
            "Available subagent types: " + ", ".join(escape(name) for name in names)
        )

    def schema(self) -> dict[str, Any]:
        subagent_names = [
            agent.name for agent in self._service.registry.list(mode="subagent")
        ]
        item = {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "subagent_type": {"type": "string", "enum": subagent_names},
                "session_id": {"type": "string"},
                "plan_task_id": {"type": "string"},
            },
            "required": ["description", "prompt", "subagent_type"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": item,
                    "minItems": 2,
                    "maxItems": 8,
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list):
            raise ValueError("tasks must be an array")
        return await self._service.execute_task_batch(
            parent_session=self._parent_session,
            tasks=raw_tasks,
            parent_ctx=ctx,
        )

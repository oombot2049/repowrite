from __future__ import annotations

import json
from typing import Any

from nexapilot.model import CreateGoalRequest, PlanTaskSpec
from nexapilot.task_runtime import TaskRuntimeService
from nexapilot.tools.base import (
    ApprovalScope, Compensation, Idempotency, RetryPolicy, SideEffect,
    ToolContract, ToolResult,
)


class TaskPlanTool:
    """Model-facing adapter for the durable Goal/Plan/Task runtime."""

    name = "taskplan"
    contract = ToolContract(
        SideEffect.LOCAL_WRITE, Idempotency.UNSAFE, RetryPolicy.NEVER,
        Compensation.MANUAL, ApprovalScope.ONCE,
    )
    description = (
        "Create, inspect, and advance a durable Goal/Plan/Task DAG. Use this for complex "
        "multi-step work that must survive restarts, express dependencies, or be audited. "
        "Use todowrite only for a lightweight visual checklist."
    )

    def __init__(self, service: TaskRuntimeService) -> None:
        self._service = service

    def schema(self) -> dict[str, Any]:
        task_spec = {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "integer", "default": 0},
                "max_attempts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 1,
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["key", "title"],
        }
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create",
                        "inspect",
                        "ready",
                        "transition",
                        "pause",
                        "resume",
                    ],
                },
                "goal_id": {"type": "string"},
                "plan_id": {"type": "string"},
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "rationale": {"type": "string"},
                "tasks": {"type": "array", "items": task_spec},
                "status": {
                    "type": "string",
                    "enum": [
                        "pending",
                        "ready",
                        "running",
                        "waiting_retry",
                        "paused",
                        "completed",
                        "failed",
                        "cancelled",
                    ],
                },
                "expected_revision": {"type": "integer", "minimum": 0},
                "reason": {"type": "string"},
                "result": {"type": "object"},
                "error": {"type": "object"},
                "retry_delay_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 86400000,
                },
            },
            "required": ["action"],
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "create"}}},
                    "then": {"required": ["title", "tasks"]},
                },
                {
                    "if": {"properties": {"action": {"const": "inspect"}}},
                    "then": {"required": ["goal_id"]},
                },
                {
                    "if": {"properties": {"action": {"const": "ready"}}},
                    "then": {"required": ["plan_id"]},
                },
                {
                    "if": {
                        "properties": {
                            "action": {"enum": ["pause", "resume"]}
                        }
                    },
                    "then": {"required": ["goal_id", "expected_revision"]},
                },
                {
                    "if": {"properties": {"action": {"const": "transition"}}},
                    "then": {
                        "required": [
                            "task_id",
                            "status",
                            "expected_revision",
                        ]
                    },
                },
            ],
        }

    @staticmethod
    def _required(args: dict[str, Any], name: str) -> str:
        value = str(args.get(name) or "").strip()
        if not value:
            raise ValueError(f"{name} is required for this action")
        return value

    async def execute(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        action = self._required(args, "action")
        actor = f"agent:{ctx.agent}"

        if action == "create":
            raw_tasks = args.get("tasks")
            if not isinstance(raw_tasks, list):
                raise ValueError("tasks is required for create")
            body = CreateGoalRequest(
                title=self._required(args, "title"),
                description=str(args.get("description") or ""),
                rationale=str(args.get("rationale") or ""),
                actor=actor,
                tasks=[PlanTaskSpec.model_validate(item) for item in raw_tasks],
            )
            result = await self._service.create_goal(ctx.session_id, body)
        elif action == "inspect":
            result = await self._service.get_goal(self._required(args, "goal_id"))
        elif action == "ready":
            ready = await self._service.ready_tasks(self._required(args, "plan_id"))
            result = {"tasks": [task.model_dump() for task in ready]}
        elif action in {"pause", "resume"}:
            if "expected_revision" not in args:
                raise ValueError("expected_revision is required for this action")
            result = await self._service.set_goal_paused(
                self._required(args, "goal_id"),
                paused=action == "pause",
                expected_revision=int(args["expected_revision"]),
                actor=actor,
                reason=str(args.get("reason") or "") or None,
            )
        elif action == "transition":
            if "expected_revision" not in args:
                raise ValueError("expected_revision is required for transition")
            result = await self._service.transition_task(
                self._required(args, "task_id"),
                target=self._required(args, "status"),
                expected_revision=int(args["expected_revision"]),
                actor=actor,
                reason=str(args.get("reason") or "") or None,
                result_payload=args.get("result"),
                error_payload=args.get("error"),
                retry_delay_ms=int(args.get("retry_delay_ms") or 0),
            )
        else:
            raise ValueError(f"unsupported action: {action}")

        output = json.dumps(result, ensure_ascii=False, indent=2)
        return ToolResult(
            title=f"Task plan: {action}",
            output=output,
            metadata={"action": action, "durable": True},
        )

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from jsonschema import Draft202012Validator

from nexapilot.bus.bus import Bus
from nexapilot.model import (
    CreateGoalRequest,
    CreatePlanRequest,
    PlanTaskSpec,
    Session,
)
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.task_runtime import (
    TaskRuntimeConflict,
    TaskRuntimeService,
    TaskRuntimeValidationError,
)
from nexapilot.tools.taskplan import TaskPlanTool


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "runtime.sqlite3")
        self.store = SQLiteStore(self.db_path)
        self.bus = Bus()
        self.runtime = TaskRuntimeService(self.db_path, self.bus)
        await self.runtime.init()
        now = int(time.time() * 1000)
        self.session = Session(
            id=str(uuid4()),
            title="Task runtime",
            worktree=self.tmp.name,
            cwd=self.tmp.name,
            created_at=now,
            updated_at=now,
            permission_rules=[],
            root_session_id=None,
        )
        await self.store.create_session(self.session)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _linear_goal(self, *, max_attempts: int = 2) -> dict:
        return await self.runtime.create_goal(
            self.session.id,
            CreateGoalRequest(
                title="Ship durable planning",
                actor="test",
                tasks=[
                    PlanTaskSpec(
                        key="design", title="Design", max_attempts=max_attempts
                    ),
                    PlanTaskSpec(key="build", title="Build", depends_on=["design"]),
                    PlanTaskSpec(key="verify", title="Verify", depends_on=["build"]),
                ],
            ),
        )

    @staticmethod
    def _by_key(graph: dict, key: str) -> dict:
        return next(task for task in graph["tasks"] if task["key"] == key)

    async def test_create_dag_persists_ready_tasks_audit_and_outbox(self) -> None:
        graph = await self._linear_goal()
        plan_id = graph["goal"]["active_plan_id"]

        self.assertEqual(self._by_key(graph, "design")["status"], "ready")
        self.assertEqual(self._by_key(graph, "build")["status"], "pending")
        ready = await self.runtime.ready_tasks(plan_id)
        self.assertEqual([task.key for task in ready], ["design"])

        events = await self.runtime.list_events(graph["goal"]["id"])
        self.assertEqual(events[0].event_type, "goal.created")
        self.assertEqual(sum(event.event_type == "task.created" for event in events), 3)
        with closing(sqlite3.connect(self.db_path)) as db:
            outbox_count = db.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE aggregate_id=?",
                (graph["goal"]["id"],),
            ).fetchone()[0]
        self.assertEqual(outbox_count, len(events))

    async def test_invalid_cycle_is_rejected_without_partial_rows(self) -> None:
        with self.assertRaisesRegex(TaskRuntimeValidationError, "acyclic"):
            await self.runtime.create_goal(
                self.session.id,
                CreateGoalRequest(
                    title="Cycle",
                    tasks=[
                        PlanTaskSpec(key="a", title="A", depends_on=["b"]),
                        PlanTaskSpec(key="b", title="B", depends_on=["a"]),
                    ],
                ),
            )
        self.assertEqual(await self.runtime.list_goals(self.session.id), [])

    async def test_completion_unlocks_dependencies_and_finishes_goal(self) -> None:
        graph = await self._linear_goal()
        goal_id = graph["goal"]["id"]
        plan_id = graph["goal"]["active_plan_id"]

        for key in ("design", "build", "verify"):
            current = await self.runtime.get_goal(goal_id)
            task = self._by_key(current, key)
            running = await self.runtime.transition_task(
                task["id"],
                target="running",
                expected_revision=task["revision"],
                actor="agent:test",
            )
            task = self._by_key(running, key)
            completed = await self.runtime.transition_task(
                task["id"],
                target="completed",
                expected_revision=task["revision"],
                actor="agent:test",
                result_payload={"ok": True},
            )
            if key != "verify":
                ready = await self.runtime.ready_tasks(plan_id)
                expected = "build" if key == "design" else "verify"
                self.assertEqual([item.key for item in ready], [expected])

        self.assertEqual(completed["goal"]["status"], "completed")
        self.assertEqual(completed["plans"][0]["status"], "completed")
        event_types = [
            event.event_type for event in await self.runtime.list_events(goal_id)
        ]
        self.assertIn("plan.completed", event_types)
        self.assertIn("goal.completed", event_types)

    async def test_failure_cascades_to_dependents_and_fails_plan_and_goal(self) -> None:
        graph = await self._linear_goal()
        goal_id = graph["goal"]["id"]
        design = self._by_key(graph, "design")

        graph = await self.runtime.transition_task(
            design["id"],
            target="running",
            expected_revision=design["revision"],
            actor="agent:test",
        )
        design = self._by_key(graph, "design")
        graph = await self.runtime.transition_task(
            design["id"],
            target="failed",
            expected_revision=design["revision"],
            actor="agent:test",
            error_payload={"code": "provider_invalid_request"},
        )

        self.assertEqual(graph["goal"]["status"], "failed")
        self.assertEqual(graph["plans"][0]["status"], "failed")
        self.assertEqual(self._by_key(graph, "design")["status"], "failed")
        self.assertEqual(self._by_key(graph, "build")["status"], "failed")
        self.assertEqual(self._by_key(graph, "verify")["status"], "failed")
        self.assertEqual(
            self._by_key(graph, "build")["error"]["error_code"],
            "dependency_failed",
        )
        event_types = [
            event.event_type for event in await self.runtime.list_events(goal_id)
        ]
        self.assertIn("plan.failed", event_types)
        self.assertIn("goal.failed", event_types)

    async def test_pause_resume_and_revision_conflict(self) -> None:
        graph = await self._linear_goal()
        goal = graph["goal"]
        plan_id = goal["active_plan_id"]
        paused = await self.runtime.set_goal_paused(
            goal["id"],
            paused=True,
            expected_revision=goal["revision"],
            actor="user",
            reason="review",
        )
        self.assertEqual(paused["goal"]["status"], "paused")
        self.assertEqual(await self.runtime.ready_tasks(plan_id), [])

        with self.assertRaises(TaskRuntimeConflict):
            await self.runtime.set_goal_paused(
                goal["id"],
                paused=False,
                expected_revision=goal["revision"],
                actor="stale-client",
                reason=None,
            )

        resumed = await self.runtime.set_goal_paused(
            goal["id"],
            paused=False,
            expected_revision=paused["goal"]["revision"],
            actor="user",
            reason="approved",
        )
        self.assertEqual(resumed["goal"]["status"], "active")
        self.assertEqual(
            [task.key for task in await self.runtime.ready_tasks(plan_id)], ["design"]
        )

    async def test_retry_budget_and_human_takeover(self) -> None:
        graph = await self._linear_goal(max_attempts=2)
        goal_id = graph["goal"]["id"]
        task = self._by_key(graph, "design")

        graph = await self.runtime.takeover_task(
            task["id"],
            human=True,
            expected_revision=task["revision"],
            actor="user",
            assignee="alice",
            reason="manual review",
        )
        task = self._by_key(graph, "design")
        self.assertEqual(task["execution_mode"], "human")
        self.assertEqual(await self.runtime.ready_tasks(task["plan_id"]), [])
        graph = await self.runtime.takeover_task(
            task["id"],
            human=False,
            expected_revision=task["revision"],
            actor="alice",
            assignee=None,
            reason="return to agent",
        )

        task = self._by_key(graph, "design")
        graph = await self.runtime.transition_task(
            task["id"],
            target="running",
            expected_revision=task["revision"],
            actor="agent",
        )
        task = self._by_key(graph, "design")
        graph = await self.runtime.transition_task(
            task["id"],
            target="waiting_retry",
            expected_revision=task["revision"],
            actor="agent",
            error_payload={"code": "timeout"},
        )
        task = self._by_key(graph, "design")
        self.assertEqual(task["status"], "waiting_retry")
        self.assertEqual(
            [item.key for item in await self.runtime.ready_tasks(task["plan_id"])],
            ["design"],
        )
        graph = await self.runtime.get_goal(goal_id)
        task = self._by_key(graph, "design")
        graph = await self.runtime.transition_task(
            task["id"],
            target="running",
            expected_revision=task["revision"],
            actor="agent",
        )
        task = self._by_key(graph, "design")
        graph = await self.runtime.transition_task(
            task["id"],
            target="waiting_retry",
            expected_revision=task["revision"],
            actor="agent",
            error_payload={"code": "timeout"},
        )
        self.assertEqual(self._by_key(graph, "design")["status"], "failed")
        self.assertEqual(self._by_key(graph, "design")["attempt"], 2)
        self.assertEqual(graph["goal"]["id"], goal_id)

    async def test_replan_versions_history_and_reorder_is_audited(self) -> None:
        first = await self._linear_goal()
        goal = first["goal"]
        second = await self.runtime.create_plan(
            goal["id"],
            CreatePlanRequest(
                expected_goal_revision=goal["revision"],
                actor="architect",
                rationale="reduce critical path",
                tasks=[
                    PlanTaskSpec(key="a", title="A"),
                    PlanTaskSpec(key="b", title="B"),
                ],
            ),
        )
        self.assertEqual([plan["version"] for plan in second["plans"]], [2, 1])
        self.assertEqual(second["plans"][1]["status"], "superseded")
        plan = second["plans"][0]
        reversed_ids = [task["id"] for task in reversed(second["tasks"])]
        reordered = await self.runtime.reorder_plan(
            plan["id"],
            task_ids=reversed_ids,
            expected_revision=plan["revision"],
            actor="user",
            reason="priority changed",
        )
        self.assertEqual([task["id"] for task in reordered["tasks"]], reversed_ids)
        self.assertEqual(
            (await self.runtime.list_events(goal["id"]))[-1].event_type,
            "plan.reordered",
        )

    async def test_session_delete_cascades_task_runtime_rows(self) -> None:
        await self._linear_goal()
        await self.store.delete_session(self.session.id)
        with closing(sqlite3.connect(self.db_path)) as db:
            for table in (
                "goals",
                "task_plans",
                "plan_tasks",
                "plan_task_dependencies",
                "task_runtime_events",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )

    async def test_agent_tool_creates_and_advances_durable_plan(self) -> None:
        tool = TaskPlanTool(self.runtime)
        ctx = SimpleNamespace(session_id=self.session.id, agent="primary")
        created = await tool.execute(
            {
                "action": "create",
                "title": "Tool-driven goal",
                "tasks": [
                    {"key": "change", "title": "Change code"},
                    {"key": "test", "title": "Run tests", "depends_on": ["change"]},
                ],
            },
            ctx,
        )
        graph = json.loads(created.output)
        self.assertTrue(created.metadata["durable"])
        task = self._by_key(graph, "change")
        running = await tool.execute(
            {
                "action": "transition",
                "task_id": task["id"],
                "status": "running",
                "expected_revision": task["revision"],
            },
            ctx,
        )
        running_graph = json.loads(running.output)
        self.assertEqual(self._by_key(running_graph, "change")["status"], "running")

    def test_taskplan_schema_requires_revision_for_mutations(self) -> None:
        validator = Draft202012Validator(TaskPlanTool(self.runtime).schema())

        missing = list(
            validator.iter_errors({"action": "pause", "goal_id": "goal-1"})
        )
        valid = list(
            validator.iter_errors(
                {
                    "action": "pause",
                    "goal_id": "goal-1",
                    "expected_revision": 3,
                }
            )
        )

        self.assertTrue(any("expected_revision" in error.message for error in missing))
        self.assertEqual(valid, [])

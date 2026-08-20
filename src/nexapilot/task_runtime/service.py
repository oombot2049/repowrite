from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Any, ClassVar
from uuid import uuid4

import aiosqlite

from nexapilot.bus.bus import Bus, Event
from nexapilot.model import (
    CreateGoalRequest,
    CreatePlanRequest,
    Goal,
    OutboxEvent,
    PlanTask,
    PlanTaskSpec,
    TaskPlan,
    TaskRuntimeEvent,
)
from nexapilot.outbox import OutboxWorker
from nexapilot.store.sqlite import SQLiteStore


class TaskRuntimeError(ValueError):
    pass


class TaskRuntimeNotFound(TaskRuntimeError):
    pass


class TaskRuntimeValidationError(TaskRuntimeError):
    pass


class TaskRuntimeConflict(TaskRuntimeError):
    pass


class TaskRuntimeService:
    """Durable Goal -> Plan -> Task DAG runtime.

    Snapshot updates, audit events, and transactional-outbox rows are committed in
    one SQLite transaction. The in-process Bus is only notified after commit.
    """

    TASK_TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "pending": {"ready", "paused", "cancelled"},
        "ready": {"running", "completed", "paused", "cancelled"},
        "running": {"completed", "waiting_retry", "failed", "paused", "cancelled"},
        "waiting_retry": {"ready", "paused", "failed", "cancelled"},
        "paused": {"pending", "ready", "cancelled"},
        "failed": {"ready", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    }
    EVENT_TYPES: ClassVar[set[str]] = {
        "goal.created",
        "goal.paused",
        "goal.resumed",
        "goal.completed",
        "goal.failed",
        "plan.activated",
        "plan.reordered",
        "plan.completed",
        "plan.failed",
        "task.created",
        "task.ready",
        "task.running",
        "task.completed",
        "task.waiting_retry",
        "task.failed",
        "task.paused",
        "task.cancelled",
        "task.taken_over",
        "task.released_to_agent",
    }

    def __init__(self, db_path: str, bus: Bus) -> None:
        self._path = db_path
        self._bus = bus
        self._outbox_worker = OutboxWorker(
            store=SQLiteStore(db_path),
            worker_id=f"task-runtime-{uuid4().hex[:8]}",
            handlers={
                event_type: self._replay_event for event_type in self.EVENT_TYPES
            },
        )

    async def start(self) -> None:
        await self._outbox_worker.start()

    async def stop(self) -> None:
        await self._outbox_worker.stop()

    async def _replay_event(self, event: OutboxEvent) -> None:
        await self._bus.publish(
            Event(
                type=event.event_type,
                properties=event.payload | {"session_id": event.session_id},
            )
        )

    async def init(self) -> None:
        await SQLiteStore(self._path).init()

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _validate_specs(specs: list[PlanTaskSpec]) -> None:
        keys = [spec.key.strip() for spec in specs]
        if any(not key for key in keys):
            raise TaskRuntimeValidationError("task key is required")
        if len(set(keys)) != len(keys):
            raise TaskRuntimeValidationError("task keys must be unique within a plan")
        known = set(keys)
        edges: dict[str, set[str]] = {}
        reverse: dict[str, set[str]] = defaultdict(set)
        for spec in specs:
            dependencies = set(spec.depends_on)
            if spec.key in dependencies:
                raise TaskRuntimeValidationError(
                    f"task '{spec.key}' cannot depend on itself"
                )
            unknown = dependencies - known
            if unknown:
                raise TaskRuntimeValidationError(
                    f"task '{spec.key}' has unknown dependencies: {', '.join(sorted(unknown))}"
                )
            edges[spec.key] = dependencies
            for dependency in dependencies:
                reverse[dependency].add(spec.key)

        indegree = {key: len(edges[key]) for key in keys}
        queue = deque(key for key in keys if indegree[key] == 0)
        visited = 0
        while queue:
            key = queue.popleft()
            visited += 1
            for child in reverse[key]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if visited != len(keys):
            raise TaskRuntimeValidationError(
                "task dependencies must form an acyclic graph"
            )

    async def _append_event(
        self,
        db: aiosqlite.Connection,
        *,
        session_id: str,
        goal_id: str,
        event_type: str,
        actor: str,
        plan_id: str | None = None,
        task_id: str | None = None,
        reason: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: int,
    ) -> dict[str, Any]:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_runtime_events WHERE goal_id=?",
            (goal_id,),
        )
        row = await cursor.fetchone()
        sequence = int(row[0])
        event_id = str(uuid4())
        body = payload or {}
        await db.execute(
            """
            INSERT INTO task_runtime_events (
              id,sequence,session_id,goal_id,plan_id,task_id,event_type,actor,reason,
              from_status,to_status,run_id,payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                sequence,
                session_id,
                goal_id,
                plan_id,
                task_id,
                event_type,
                actor,
                reason,
                from_status,
                to_status,
                run_id,
                json.dumps(body, ensure_ascii=False),
                now,
            ),
        )
        outbox_payload = {
            "event_id": event_id,
            "sequence": sequence,
            "goal_id": goal_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "actor": actor,
            "reason": reason,
            "from_status": from_status,
            "to_status": to_status,
            **body,
        }
        await db.execute(
            """
            INSERT INTO outbox_events (
              id,idempotency_key,event_type,aggregate_type,aggregate_id,session_id,
              run_id,payload_json,status,attempts,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid4()),
                f"task-runtime:{event_id}",
                event_type,
                "goal",
                goal_id,
                session_id,
                run_id,
                json.dumps(outbox_payload, ensure_ascii=False),
                "pending",
                0,
                now,
            ),
        )
        return {
            "type": event_type,
            "properties": outbox_payload | {"session_id": session_id},
        }

    async def _publish(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        while await self._outbox_worker.run_once() > 0:
            pass

    async def _insert_plan(
        self,
        db: aiosqlite.Connection,
        *,
        goal_id: str,
        version: int,
        rationale: str,
        source_run_id: str | None,
        specs: list[PlanTaskSpec],
        now: int,
    ) -> tuple[str, list[tuple[str, PlanTaskSpec]]]:
        plan_id = str(uuid4())
        await db.execute(
            """
            INSERT INTO task_plans (id,goal_id,version,status,rationale,source_run_id,revision,created_at,updated_at)
            VALUES (?,?,?,'active',?,?,0,?,?)
            """,
            (plan_id, goal_id, version, rationale.strip(), source_run_id, now, now),
        )
        tasks: list[tuple[str, PlanTaskSpec]] = []
        by_key: dict[str, str] = {}
        for position, spec in enumerate(specs):
            task_id = str(uuid4())
            by_key[spec.key] = task_id
            tasks.append((task_id, spec))
            status = "ready" if not spec.depends_on else "pending"
            await db.execute(
                """
                INSERT INTO plan_tasks (
                  id,plan_id,task_key,title,description,status,priority,position,max_attempts,
                  revision,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)
                """,
                (
                    task_id,
                    plan_id,
                    spec.key,
                    spec.title.strip(),
                    spec.description.strip(),
                    status,
                    spec.priority,
                    position,
                    spec.max_attempts,
                    now,
                    now,
                ),
            )
        for task_id, spec in tasks:
            for dependency in spec.depends_on:
                await db.execute(
                    "INSERT INTO plan_task_dependencies (task_id,depends_on_task_id) VALUES (?,?)",
                    (task_id, by_key[dependency]),
                )
        return plan_id, tasks

    async def create_goal(
        self, session_id: str, body: CreateGoalRequest
    ) -> dict[str, Any]:
        self._validate_specs(body.tasks)
        now = self._now()
        goal_id = str(uuid4())
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            session = await db.execute(
                "SELECT 1 FROM sessions WHERE id=?", (session_id,)
            )
            if not await session.fetchone():
                raise TaskRuntimeNotFound(f"session not found: {session_id}")
            await db.execute(
                """
                INSERT INTO goals (id,session_id,title,description,status,revision,created_at,updated_at)
                VALUES (?,?,?,?,'active',0,?,?)
                """,
                (
                    goal_id,
                    session_id,
                    body.title.strip(),
                    body.description.strip(),
                    now,
                    now,
                ),
            )
            plan_id, tasks = await self._insert_plan(
                db,
                goal_id=goal_id,
                version=1,
                rationale=body.rationale,
                source_run_id=body.source_run_id,
                specs=body.tasks,
                now=now,
            )
            await db.execute(
                "UPDATE goals SET active_plan_id=? WHERE id=?", (plan_id, goal_id)
            )
            events.append(
                await self._append_event(
                    db,
                    session_id=session_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    event_type="goal.created",
                    actor=body.actor,
                    payload={"title": body.title, "plan_version": 1},
                    run_id=body.source_run_id,
                    now=now,
                )
            )
            for task_id, spec in tasks:
                events.append(
                    await self._append_event(
                        db,
                        session_id=session_id,
                        goal_id=goal_id,
                        plan_id=plan_id,
                        task_id=task_id,
                        event_type="task.created",
                        actor=body.actor,
                        to_status="ready" if not spec.depends_on else "pending",
                        payload={"key": spec.key, "depends_on": spec.depends_on},
                        run_id=body.source_run_id,
                        now=now,
                    )
                )
            await db.commit()
        await self._publish(events)
        return await self.get_goal(goal_id)

    async def create_plan(
        self, goal_id: str, body: CreatePlanRequest
    ) -> dict[str, Any]:
        self._validate_specs(body.tasks)
        now = self._now()
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT session_id,status,active_plan_id,revision FROM goals WHERE id=?",
                (goal_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"goal not found: {goal_id}")
            session_id, goal_status, previous_plan_id, revision = row
            if goal_status in {"completed", "cancelled"}:
                raise TaskRuntimeValidationError(f"cannot replan a {goal_status} goal")
            if int(revision) != body.expected_goal_revision:
                raise TaskRuntimeConflict(
                    f"goal revision conflict: expected {body.expected_goal_revision}, current {revision}"
                )
            version_cursor = await db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM task_plans WHERE goal_id=?",
                (goal_id,),
            )
            version = int((await version_cursor.fetchone())[0])
            plan_id, tasks = await self._insert_plan(
                db,
                goal_id=goal_id,
                version=version,
                rationale=body.rationale,
                source_run_id=body.source_run_id,
                specs=body.tasks,
                now=now,
            )
            if previous_plan_id:
                await db.execute(
                    "UPDATE task_plans SET status='superseded',revision=revision+1,updated_at=? WHERE id=?",
                    (now, previous_plan_id),
                )
            result = await db.execute(
                """
                UPDATE goals SET active_plan_id=?,revision=revision+1,updated_at=?
                WHERE id=? AND revision=?
                """,
                (plan_id, now, goal_id, body.expected_goal_revision),
            )
            if result.rowcount != 1:
                raise TaskRuntimeConflict("goal changed while creating plan")
            events.append(
                await self._append_event(
                    db,
                    session_id=session_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    event_type="plan.activated",
                    actor=body.actor,
                    payload={
                        "version": version,
                        "superseded_plan_id": previous_plan_id,
                    },
                    run_id=body.source_run_id,
                    now=now,
                )
            )
            for task_id, spec in tasks:
                events.append(
                    await self._append_event(
                        db,
                        session_id=session_id,
                        goal_id=goal_id,
                        plan_id=plan_id,
                        task_id=task_id,
                        event_type="task.created",
                        actor=body.actor,
                        to_status="ready" if not spec.depends_on else "pending",
                        payload={"key": spec.key, "depends_on": spec.depends_on},
                        run_id=body.source_run_id,
                        now=now,
                    )
                )
            await db.commit()
        await self._publish(events)
        return await self.get_goal(goal_id)

    @staticmethod
    def _goal(row: tuple[Any, ...]) -> Goal:
        return Goal(
            id=row[0],
            session_id=row[1],
            title=row[2],
            description=row[3],
            status=row[4],
            active_plan_id=row[5],
            revision=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

    @staticmethod
    def _plan(row: tuple[Any, ...]) -> TaskPlan:
        return TaskPlan(
            id=row[0],
            goal_id=row[1],
            version=row[2],
            status=row[3],
            rationale=row[4],
            source_run_id=row[5],
            revision=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

    @staticmethod
    def _task(row: tuple[Any, ...]) -> PlanTask:
        return PlanTask(
            id=row[0],
            plan_id=row[1],
            key=row[2],
            title=row[3],
            description=row[4],
            status=row[5],
            priority=row[6],
            position=row[7],
            execution_mode=row[8],
            assignee=row[9],
            attempt=row[10],
            max_attempts=row[11],
            next_attempt_at=row[12],
            owner_id=row[13],
            lease_until=row[14],
            last_run_id=row[15],
            result=json.loads(row[16]) if row[16] else None,
            error=json.loads(row[17]) if row[17] else None,
            revision=row[18],
            created_at=row[19],
            updated_at=row[20],
        )

    async def list_goals(self, session_id: str) -> list[Goal]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """SELECT id,session_id,title,description,status,active_plan_id,revision,created_at,updated_at
                   FROM goals WHERE session_id=? ORDER BY updated_at DESC""",
                (session_id,),
            )
            return [self._goal(row) for row in await cursor.fetchall()]

    async def get_goal(self, goal_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """SELECT id,session_id,title,description,status,active_plan_id,revision,created_at,updated_at
                   FROM goals WHERE id=?""",
                (goal_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"goal not found: {goal_id}")
            goal = self._goal(row)
            plans_cursor = await db.execute(
                """SELECT id,goal_id,version,status,rationale,source_run_id,revision,created_at,updated_at
                   FROM task_plans WHERE goal_id=? ORDER BY version DESC""",
                (goal_id,),
            )
            plans = [self._plan(item) for item in await plans_cursor.fetchall()]
            tasks: list[PlanTask] = []
            dependencies: list[dict[str, str]] = []
            if goal.active_plan_id:
                tasks_cursor = await db.execute(
                    """SELECT id,plan_id,task_key,title,description,status,priority,position,
                              execution_mode,assignee,attempt,max_attempts,next_attempt_at,owner_id,
                              lease_until,last_run_id,result_json,error_json,revision,created_at,updated_at
                       FROM plan_tasks WHERE plan_id=? ORDER BY position""",
                    (goal.active_plan_id,),
                )
                tasks = [self._task(item) for item in await tasks_cursor.fetchall()]
                dep_cursor = await db.execute(
                    """SELECT d.task_id,d.depends_on_task_id
                       FROM plan_task_dependencies d JOIN plan_tasks t ON t.id=d.task_id
                       WHERE t.plan_id=? ORDER BY d.task_id,d.depends_on_task_id""",
                    (goal.active_plan_id,),
                )
                dependencies = [
                    {"task_id": item[0], "depends_on_task_id": item[1]}
                    for item in await dep_cursor.fetchall()
                ]
            return {
                "goal": goal.model_dump(),
                "plans": [plan.model_dump() for plan in plans],
                "tasks": [task.model_dump() for task in tasks],
                "dependencies": dependencies,
            }

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Return one durable task together with its owning goal/session.

        Delegated agent tasks use this lookup to validate that a model-provided
        task id belongs to the current root session before changing its state.
        """
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """SELECT t.id,t.plan_id,t.task_key,t.title,t.description,t.status,
                          t.priority,t.position,t.execution_mode,t.assignee,t.attempt,
                          t.max_attempts,t.next_attempt_at,t.owner_id,t.lease_until,
                          t.last_run_id,t.result_json,t.error_json,t.revision,
                          t.created_at,t.updated_at,p.goal_id,g.session_id
                   FROM plan_tasks t
                   JOIN task_plans p ON p.id=t.plan_id
                   JOIN goals g ON g.id=p.goal_id
                   WHERE t.id=?""",
                (task_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"task not found: {task_id}")
            return {
                "task": self._task(row[:21]),
                "goal_id": row[21],
                "session_id": row[22],
            }

    async def list_events(self, goal_id: str) -> list[TaskRuntimeEvent]:
        async with aiosqlite.connect(self._path) as db:
            exists = await db.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,))
            if not await exists.fetchone():
                raise TaskRuntimeNotFound(f"goal not found: {goal_id}")
            cursor = await db.execute(
                """SELECT id,sequence,session_id,goal_id,plan_id,task_id,event_type,actor,reason,
                          from_status,to_status,run_id,payload_json,created_at
                   FROM task_runtime_events WHERE goal_id=? ORDER BY sequence""",
                (goal_id,),
            )
            return [
                TaskRuntimeEvent(
                    id=row[0],
                    sequence=row[1],
                    session_id=row[2],
                    goal_id=row[3],
                    plan_id=row[4],
                    task_id=row[5],
                    event_type=row[6],
                    actor=row[7],
                    reason=row[8],
                    from_status=row[9],
                    to_status=row[10],
                    run_id=row[11],
                    payload=json.loads(row[12]),
                    created_at=row[13],
                )
                for row in await cursor.fetchall()
            ]

    async def ready_tasks(self, plan_id: str) -> list[PlanTask]:
        now = self._now()
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT g.status,p.status,p.goal_id,g.session_id
                   FROM task_plans p JOIN goals g ON g.id=p.goal_id
                   WHERE p.id=?""",
                (plan_id,),
            )
            state = await cursor.fetchone()
            if not state:
                raise TaskRuntimeNotFound(f"plan not found: {plan_id}")
            if state[0] != "active" or state[1] != "active":
                await db.rollback()
                return []
            goal_id, session_id = state[2], state[3]
            due_cursor = await db.execute(
                """SELECT id FROM plan_tasks
                   WHERE plan_id=? AND status='waiting_retry' AND execution_mode='agent'
                     AND next_attempt_at IS NOT NULL AND next_attempt_at<=?""",
                (plan_id, now),
            )
            for (task_id,) in await due_cursor.fetchall():
                blocked_cursor = await db.execute(
                    """SELECT COUNT(*) FROM plan_task_dependencies d
                       JOIN plan_tasks parent ON parent.id=d.depends_on_task_id
                       WHERE d.task_id=? AND parent.status<>'completed'""",
                    (task_id,),
                )
                if int((await blocked_cursor.fetchone())[0]) > 0:
                    continue
                await db.execute(
                    """UPDATE plan_tasks SET status='ready',next_attempt_at=NULL,
                              revision=revision+1,updated_at=? WHERE id=?""",
                    (now, task_id),
                )
                events.append(
                    await self._append_event(
                        db,
                        session_id=session_id,
                        goal_id=goal_id,
                        plan_id=plan_id,
                        task_id=task_id,
                        event_type="task.ready",
                        actor="scheduler",
                        reason="retry_delay_elapsed",
                        from_status="waiting_retry",
                        to_status="ready",
                        now=now,
                    )
                )
            cursor = await db.execute(
                """SELECT id,plan_id,task_key,title,description,status,priority,position,
                          execution_mode,assignee,attempt,max_attempts,next_attempt_at,owner_id,
                          lease_until,last_run_id,result_json,error_json,revision,created_at,updated_at
                   FROM plan_tasks
                   WHERE plan_id=? AND status='ready' AND execution_mode='agent'
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   ORDER BY priority DESC,position ASC""",
                (plan_id, now),
            )
            tasks = [self._task(row) for row in await cursor.fetchall()]
            await db.commit()
        await self._publish(events)
        return tasks

    async def set_goal_paused(
        self,
        goal_id: str,
        *,
        paused: bool,
        expected_revision: int,
        actor: str,
        reason: str | None,
    ) -> dict[str, Any]:
        now = self._now()
        target = "paused" if paused else "active"
        expected_status = "active" if paused else "paused"
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT session_id,status,active_plan_id,revision FROM goals WHERE id=?",
                (goal_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"goal not found: {goal_id}")
            session_id, status, plan_id, revision = row
            if int(revision) != expected_revision:
                raise TaskRuntimeConflict(
                    f"goal revision conflict: expected {expected_revision}, current {revision}"
                )
            if status != expected_status:
                raise TaskRuntimeValidationError(
                    f"cannot change goal from {status} to {target}"
                )
            result = await db.execute(
                "UPDATE goals SET status=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (target, now, goal_id, expected_revision),
            )
            if result.rowcount != 1:
                raise TaskRuntimeConflict("goal changed while updating")
            events.append(
                await self._append_event(
                    db,
                    session_id=session_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    event_type="goal.paused" if paused else "goal.resumed",
                    actor=actor,
                    reason=reason,
                    from_status=status,
                    to_status=target,
                    now=now,
                )
            )
            await db.commit()
        await self._publish(events)
        return await self.get_goal(goal_id)

    async def reorder_plan(
        self,
        plan_id: str,
        *,
        task_ids: list[str],
        expected_revision: int,
        actor: str,
        reason: str | None,
    ) -> dict[str, Any]:
        if len(set(task_ids)) != len(task_ids):
            raise TaskRuntimeValidationError("task_ids must be unique")
        now = self._now()
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT p.goal_id,p.status,p.revision,g.session_id
                   FROM task_plans p JOIN goals g ON g.id=p.goal_id WHERE p.id=?""",
                (plan_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"plan not found: {plan_id}")
            goal_id, status, revision, session_id = row
            if int(revision) != expected_revision:
                raise TaskRuntimeConflict(
                    f"plan revision conflict: expected {expected_revision}, current {revision}"
                )
            if status != "active":
                raise TaskRuntimeValidationError("only an active plan can be reordered")
            task_cursor = await db.execute(
                "SELECT id FROM plan_tasks WHERE plan_id=?", (plan_id,)
            )
            existing = {item[0] for item in await task_cursor.fetchall()}
            if set(task_ids) != existing:
                raise TaskRuntimeValidationError(
                    "task_ids must contain every task in the plan exactly once"
                )
            for position, task_id in enumerate(task_ids):
                await db.execute(
                    "UPDATE plan_tasks SET position=?,revision=revision+1,updated_at=? WHERE id=?",
                    (position, now, task_id),
                )
            result = await db.execute(
                "UPDATE task_plans SET revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (now, plan_id, expected_revision),
            )
            if result.rowcount != 1:
                raise TaskRuntimeConflict("plan changed while reordering")
            events.append(
                await self._append_event(
                    db,
                    session_id=session_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    event_type="plan.reordered",
                    actor=actor,
                    reason=reason,
                    payload={"task_ids": task_ids},
                    now=now,
                )
            )
            await db.commit()
        await self._publish(events)
        return await self.get_goal(goal_id)

    async def transition_task(
        self,
        task_id: str,
        *,
        target: str,
        expected_revision: int,
        actor: str,
        reason: str | None = None,
        run_id: str | None = None,
        result_payload: dict[str, Any] | None = None,
        error_payload: dict[str, Any] | None = None,
        retry_delay_ms: int = 0,
    ) -> dict[str, Any]:
        now = self._now()
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT t.plan_id,t.status,t.revision,t.attempt,t.max_attempts,t.execution_mode,
                          p.goal_id,g.session_id
                   FROM plan_tasks t JOIN task_plans p ON p.id=t.plan_id JOIN goals g ON g.id=p.goal_id
                   WHERE t.id=?""",
                (task_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"task not found: {task_id}")
            (
                plan_id,
                current,
                revision,
                attempt,
                max_attempts,
                mode,
                goal_id,
                session_id,
            ) = row
            if int(revision) != expected_revision:
                raise TaskRuntimeConflict(
                    f"task revision conflict: expected {expected_revision}, current {revision}"
                )
            if target not in self.TASK_TRANSITIONS.get(current, set()):
                raise TaskRuntimeValidationError(
                    f"invalid task transition: {current} -> {target}"
                )
            if target == "running" and mode != "agent":
                raise TaskRuntimeValidationError(
                    "a human-owned task cannot be started by the agent"
                )
            if target == "completed" and current == "ready" and mode != "human":
                raise TaskRuntimeValidationError(
                    "an agent task must be running before completion"
                )
            if target == "ready":
                dep_cursor = await db.execute(
                    """SELECT COUNT(*) FROM plan_task_dependencies d
                       JOIN plan_tasks parent ON parent.id=d.depends_on_task_id
                       WHERE d.task_id=? AND parent.status<>'completed'""",
                    (task_id,),
                )
                if int((await dep_cursor.fetchone())[0]) > 0:
                    raise TaskRuntimeValidationError(
                        "task dependencies are not complete"
                    )
            next_attempt_at = (
                now + retry_delay_ms if target == "waiting_retry" else None
            )
            new_attempt = int(attempt) + 1 if target == "running" else int(attempt)
            if target == "waiting_retry" and new_attempt >= int(max_attempts):
                target = "failed"
                next_attempt_at = None
            update = await db.execute(
                """UPDATE plan_tasks SET status=?,attempt=?,next_attempt_at=?,last_run_id=COALESCE(?,last_run_id),
                          result_json=?,error_json=?,owner_id=?,lease_until=?,revision=revision+1,updated_at=?
                   WHERE id=? AND revision=?""",
                (
                    target,
                    new_attempt,
                    next_attempt_at,
                    run_id,
                    json.dumps(result_payload, ensure_ascii=False)
                    if result_payload is not None
                    else None,
                    json.dumps(error_payload, ensure_ascii=False)
                    if error_payload is not None
                    else None,
                    actor if target == "running" else None,
                    now + 60_000 if target == "running" else None,
                    now,
                    task_id,
                    expected_revision,
                ),
            )
            if update.rowcount != 1:
                raise TaskRuntimeConflict("task changed while transitioning")
            events.append(
                await self._append_event(
                    db,
                    session_id=session_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    task_id=task_id,
                    event_type=f"task.{target}",
                    actor=actor,
                    reason=reason,
                    from_status=current,
                    to_status=target,
                    run_id=run_id,
                    payload={
                        "attempt": new_attempt,
                        "next_attempt_at": next_attempt_at,
                    },
                    now=now,
                )
            )
            if target == "completed":
                child_cursor = await db.execute(
                    """SELECT child.id,child.status FROM plan_task_dependencies d
                       JOIN plan_tasks child ON child.id=d.task_id
                       WHERE d.depends_on_task_id=?""",
                    (task_id,),
                )
                for child_id, child_status in await child_cursor.fetchall():
                    if child_status != "pending":
                        continue
                    blocked_cursor = await db.execute(
                        """SELECT COUNT(*) FROM plan_task_dependencies d
                           JOIN plan_tasks parent ON parent.id=d.depends_on_task_id
                           WHERE d.task_id=? AND parent.status<>'completed'""",
                        (child_id,),
                    )
                    if int((await blocked_cursor.fetchone())[0]) == 0:
                        await db.execute(
                            "UPDATE plan_tasks SET status='ready',revision=revision+1,updated_at=? WHERE id=?",
                            (now, child_id),
                        )
                        events.append(
                            await self._append_event(
                                db,
                                session_id=session_id,
                                goal_id=goal_id,
                                plan_id=plan_id,
                                task_id=child_id,
                                event_type="task.ready",
                                actor="system",
                                reason="dependencies_completed",
                                from_status="pending",
                                to_status="ready",
                                now=now,
                            )
                        )
            if target in {"failed", "cancelled"}:
                # A dependency failure otherwise leaves descendants permanently
                # pending and the Goal permanently active. Close that branch
                # explicitly so every durable graph reaches a truthful terminal
                # state and remains auditable.
                blocked: set[str] = {task_id}
                changed = True
                while changed:
                    changed = False
                    dep_cursor = await db.execute(
                        "SELECT task_id,depends_on_task_id FROM plan_task_dependencies"
                    )
                    for child_id, parent_id in await dep_cursor.fetchall():
                        if parent_id in blocked and child_id not in blocked:
                            blocked.add(child_id)
                            changed = True
                for child_id in blocked - {task_id}:
                    child_cursor = await db.execute(
                        "SELECT status FROM plan_tasks WHERE id=? AND plan_id=?",
                        (child_id, plan_id),
                    )
                    child_row = await child_cursor.fetchone()
                    if not child_row or child_row[0] not in {"pending", "ready"}:
                        continue
                    dependency_error = {
                        "error_code": "dependency_failed",
                        "dependency_task_id": task_id,
                    }
                    await db.execute(
                        """UPDATE plan_tasks
                           SET status='failed',error_json=?,revision=revision+1,updated_at=?
                           WHERE id=?""",
                        (json.dumps(dependency_error), now, child_id),
                    )
                    events.append(
                        await self._append_event(
                            db,
                            session_id=session_id,
                            goal_id=goal_id,
                            plan_id=plan_id,
                            task_id=child_id,
                            event_type="task.failed",
                            actor="system",
                            reason="dependency_failed",
                            from_status=child_row[0],
                            to_status="failed",
                            payload=dependency_error,
                            now=now,
                        )
                    )

            if target in {"completed", "failed", "cancelled"}:
                status_cursor = await db.execute(
                    "SELECT status FROM plan_tasks WHERE plan_id=?",
                    (plan_id,),
                )
                statuses = [row[0] for row in await status_cursor.fetchall()]
                terminal = {"completed", "failed", "cancelled"}
                if statuses and all(status in terminal for status in statuses):
                    final_status = (
                        "failed" if "failed" in statuses else "completed"
                    )
                    await db.execute(
                        "UPDATE task_plans SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                        (final_status, now, plan_id),
                    )
                    await db.execute(
                        "UPDATE goals SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                        (final_status, now, goal_id),
                    )
                    events.append(
                        await self._append_event(
                            db,
                            session_id=session_id,
                            goal_id=goal_id,
                            plan_id=plan_id,
                            event_type=f"plan.{final_status}",
                            actor="system",
                            reason="all_tasks_terminal",
                            from_status="active",
                            to_status=final_status,
                            now=now,
                        )
                    )
                    events.append(
                        await self._append_event(
                            db,
                            session_id=session_id,
                            goal_id=goal_id,
                            plan_id=plan_id,
                            event_type=f"goal.{final_status}",
                            actor="system",
                            reason=f"active_plan_{final_status}",
                            from_status="active",
                            to_status=final_status,
                            now=now,
                        )
                    )
            await db.commit()
        await self._publish(events)
        return await self.get_goal(goal_id)

    async def takeover_task(
        self,
        task_id: str,
        *,
        human: bool,
        expected_revision: int,
        actor: str,
        assignee: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        now = self._now()
        target_mode = "human" if human else "agent"
        events: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT t.plan_id,t.status,t.execution_mode,t.revision,p.goal_id,g.session_id
                   FROM plan_tasks t JOIN task_plans p ON p.id=t.plan_id JOIN goals g ON g.id=p.goal_id
                   WHERE t.id=?""",
                (task_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise TaskRuntimeNotFound(f"task not found: {task_id}")
            plan_id, status, current_mode, revision, goal_id, session_id = row
            if int(revision) != expected_revision:
                raise TaskRuntimeConflict(
                    f"task revision conflict: expected {expected_revision}, current {revision}"
                )
            if status in {"completed", "cancelled"}:
                raise TaskRuntimeValidationError(
                    f"cannot change ownership of a {status} task"
                )
            if current_mode == target_mode:
                raise TaskRuntimeValidationError(
                    f"task is already in {target_mode} mode"
                )
            if human and not assignee:
                raise TaskRuntimeValidationError(
                    "assignee is required for human takeover"
                )
            update = await db.execute(
                """UPDATE plan_tasks SET execution_mode=?,assignee=?,owner_id=NULL,lease_until=NULL,
                          revision=revision+1,updated_at=? WHERE id=? AND revision=?""",
                (
                    target_mode,
                    assignee if human else None,
                    now,
                    task_id,
                    expected_revision,
                ),
            )
            if update.rowcount != 1:
                raise TaskRuntimeConflict("task changed while updating ownership")
            event_type = "task.taken_over" if human else "task.released_to_agent"
            events.append(
                await self._append_event(
                    db,
                    session_id=session_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    task_id=task_id,
                    event_type=event_type,
                    actor=actor,
                    reason=reason,
                    payload={
                        "execution_mode": target_mode,
                        "assignee": assignee if human else None,
                    },
                    now=now,
                )
            )
            await db.commit()
        await self._publish(events)
        return await self.get_goal(goal_id)

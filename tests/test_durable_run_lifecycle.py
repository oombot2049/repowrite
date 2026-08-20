from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from nexapilot.bus.bus import Bus
from nexapilot.model import (
    Message,
    ModelRef,
    PermissionRule,
    PermissionReply,
    Session,
    TextPart,
    ToolPart,
    ToolStateRunning,
)
from nexapilot.permission.service import PermissionService
from nexapilot.run_lifecycle import validate_run_transition
from nexapilot.store.sqlite import SQLiteStore


class DurableRunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "runs.sqlite3")
        self.store = SQLiteStore(self.db_path)
        await self.store.init()
        self.session = Session(
            id=str(uuid4()),
            title="durable run test",
            worktree=self.tempdir.name,
            cwd=self.tempdir.name,
            created_at=int(time.time() * 1000),
            updated_at=int(time.time() * 1000),
            permission_rules=[
                PermissionRule(permission="*", pattern="*", action="ask")
            ],
        )
        await self.store.create_session(self.session)
        self.model = ModelRef(provider="openai-compatible", id="test-model")

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _create_user_message(self, text: str = "do work") -> Message:
        message = Message(
            id=str(uuid4()),
            session_id=self.session.id,
            role="user",
            agent="primary",
            model=self.model,
            created_at=int(time.time() * 1000),
        )
        message = await self.store.add_message(message)
        await self.store.add_part(
            self.session.id,
            message.id,
            TextPart(
                id=str(uuid4()),
                message_id=message.id,
                session_id=self.session.id,
                text=text,
            ),
        )
        return message

    async def _create_started_run(self, *, owner_id: str = "worker-old"):
        user = await self._create_user_message()
        run = await self.store.create_run(
            session_id=self.session.id,
            trigger_message_id=user.id,
            source="test",
            agent_name="primary",
            model=self.model,
        )
        sequence = await self.store.attach_message_to_run(user.id, run.id)
        return await self.store.start_run(
            run.id,
            input_sequence=sequence,
            owner_id=owner_id,
            lease_duration_ms=20_000,
        )

    def test_state_machine_rejects_terminal_and_illegal_transitions(self) -> None:
        validate_run_transition("queued", "running")
        validate_run_transition("running", "waiting_approval")
        validate_run_transition("waiting_approval", "running")
        validate_run_transition("cancelling", "cancelled")
        with self.assertRaisesRegex(ValueError, "illegal run transition"):
            validate_run_transition("completed", "running")
        with self.assertRaisesRegex(ValueError, "illegal run transition"):
            validate_run_transition("queued", "completed")

    async def test_session_lease_prevents_concurrent_run_and_is_released(self) -> None:
        first = await self._create_started_run(owner_id="worker-a")
        self.assertEqual(first.status, "running")
        self.assertEqual(first.owner_id, "worker-a")
        self.assertEqual(first.revision, 1)

        second_user = await self._create_user_message("second")
        second = await self.store.create_run(
            session_id=self.session.id,
            trigger_message_id=second_user.id,
            source="test",
            agent_name="primary",
            model=self.model,
        )
        second_sequence = await self.store.attach_message_to_run(
            second_user.id, second.id
        )
        with self.assertRaisesRegex(RuntimeError, "active run"):
            await self.store.start_run(
                second.id,
                input_sequence=second_sequence,
                owner_id="worker-b",
            )

        await self.store.finish_run(
            first.id,
            status="completed",
            assistant_message_id=None,
            finish_reason="stop",
        )
        started_second = await self.store.start_run(
            second.id,
            input_sequence=second_sequence,
            owner_id="worker-b",
        )
        self.assertEqual(started_second.owner_id, "worker-b")

    async def test_heartbeat_requires_current_owner(self) -> None:
        run = await self._create_started_run(owner_id="worker-a")
        previous_revision = run.revision
        self.assertFalse(
            await self.store.heartbeat_run(
                run.id, owner_id="worker-b", lease_duration_ms=20_000
            )
        )
        self.assertTrue(
            await self.store.heartbeat_run(
                run.id, owner_id="worker-a", lease_duration_ms=20_000
            )
        )
        refreshed = await self.store.get_run(run.id)
        self.assertGreater(refreshed.revision, previous_revision)
        self.assertIsNotNone(refreshed.heartbeat_at)

    async def test_startup_reconciliation_terminalizes_all_durable_facts(self) -> None:
        run = await self._create_started_run(owner_id="worker-old")
        assistant = Message(
            id=str(uuid4()),
            session_id=self.session.id,
            run_id=run.id,
            role="assistant",
            parent_id=run.trigger_message_id,
            agent="primary",
            model=self.model,
            created_at=int(time.time() * 1000),
        )
        assistant = await self.store.add_message(assistant)
        part_id = str(uuid4())
        call_id = "call-crashed"
        await self.store.add_part(
            self.session.id,
            assistant.id,
            ToolPart(
                id=part_id,
                message_id=assistant.id,
                session_id=self.session.id,
                call_id=call_id,
                tool="bash",
                state=ToolStateRunning(
                    input={"command": "echo durable"},
                    time={"start": int(time.time() * 1000)},
                ),
            ),
        )
        step = await self.store.create_run_step(
            run_id=run.id,
            kind="tool_batch",
            input_ref=assistant.id,
        )
        await self.store.upsert_tool_operation(
            {
                "operation_id": "operation-crashed",
                "run_id": run.id,
                "session_id": self.session.id,
                "message_id": assistant.id,
                "tool_call_id": call_id,
                "tool_name": "bash",
                "capability": "process.exec.shell",
                "canonical_target": "echo durable",
                "executor_backend": "local_guarded",
                "isolation_level": "guarded_host",
                "status": "executing",
                "error_code": None,
                "input": {"command": "echo durable"},
                "result": {},
                "created_at": run.started_at or run.created_at,
                "finished_at": run.started_at or run.created_at,
            }
        )

        recovered = await self.store.reconcile_abandoned_runs(
            owner_id="worker-new",
            stale_after_ms=1_000,
            now_ms=(run.lease_until or 0) + 1,
        )
        self.assertEqual([item.id for item in recovered], [run.id])
        terminal = await self.store.get_run(run.id)
        self.assertEqual(terminal.status, "interrupted")
        self.assertEqual(terminal.finish_reason, "runtime_restarted")
        self.assertEqual(terminal.error_code, "runtime_restarted")
        self.assertIsNone(terminal.owner_id)
        self.assertIsNotNone(terminal.completed_at)

        messages = await self.store.list_messages(self.session.id, run_id=run.id)
        persisted_assistant = next(
            item for item in messages if item.info.id == assistant.id
        )
        self.assertEqual(persisted_assistant.info.finish, "interrupted")
        latest_by_id = {part.id: part for part in persisted_assistant.parts}
        terminal_part = latest_by_id[part_id]
        self.assertEqual(terminal_part.state.status, "error")
        self.assertEqual(
            terminal_part.state.metadata["error_code"], "runtime_restarted"
        )
        self.assertEqual(
            terminal_part.state.metadata["side_effect_state"], "unknown"
        )

        persisted_step = await self.store.get_run_step(step.id)
        self.assertEqual(persisted_step.status, "interrupted")
        operations = await self.store.list_tool_operations(self.session.id)
        self.assertEqual(operations[0]["status"], "needs_review")
        self.assertEqual(operations[0]["error_code"], "runtime_restarted")
        self.assertEqual(
            operations[0]["result"]["metadata"]["side_effect_state"],
            "unknown",
        )

        repeated = await self.store.reconcile_abandoned_runs(
            owner_id="worker-new",
            stale_after_ms=1_000,
            now_ms=(run.lease_until or 0) + 2,
        )
        self.assertEqual(repeated, [])

    async def test_unexpired_foreign_lease_is_not_reconciled(self) -> None:
        run = await self._create_started_run(owner_id="worker-live")
        recovered = await self.store.reconcile_abandoned_runs(
            owner_id="worker-new",
            stale_after_ms=20_000,
            now_ms=(run.heartbeat_at or 0) + 1,
        )
        self.assertEqual(recovered, [])
        self.assertEqual((await self.store.get_run(run.id)).status, "running")

    async def test_cancel_request_is_persistent_and_idempotent(self) -> None:
        run = await self._create_started_run(owner_id="worker-a")
        requested = await self.store.request_run_cancel(run_id=run.id)
        self.assertIsNotNone(requested)
        assert requested is not None
        self.assertEqual(requested.status, "cancelling")
        self.assertTrue(await self.store.is_run_cancel_requested(run.id))
        repeated = await self.store.request_run_cancel(run_id=run.id)
        self.assertIsNotNone(repeated)
        self.assertEqual(repeated.cancel_requested_at, requested.cancel_requested_at)
        terminal = await self.store.finish_run(
            run.id,
            status="cancelled",
            assistant_message_id=None,
            finish_reason="user_cancelled",
        )
        self.assertEqual(terminal.status, "cancelled")
        self.assertIsNone(await self.store.request_run_cancel(run_id=run.id))

    async def test_approval_wait_is_persisted_before_event_can_be_replied(self) -> None:
        run = await self._create_started_run(owner_id="worker-a")
        assistant = Message(
            id=str(uuid4()),
            session_id=self.session.id,
            run_id=run.id,
            role="assistant",
            parent_id=run.trigger_message_id,
            agent="primary",
            model=self.model,
            created_at=int(time.time() * 1000),
        )
        assistant = await self.store.add_message(assistant)
        bus = Bus()
        permissions = PermissionService(bus, self.store)

        async def reply_to_first_event() -> None:
            async for event in bus.subscribe("permission.asked"):
                request_id = str(event.properties["id"])
                persisted = await self.store.get_run(run.id)
                self.assertEqual(persisted.status, "waiting_approval")
                await permissions.reply(
                    request_id, PermissionReply(reply="once")
                )
                return

        responder = asyncio.create_task(reply_to_first_event())
        await asyncio.sleep(0)
        await permissions.ask(
            session_id=self.session.id,
            ruleset=self.session.permission_rules,
            permission="bash",
            patterns=["echo durable"],
            metadata={},
            always=["echo durable"],
            tool={"message_id": assistant.id, "call_id": "call-approval"},
        )
        await responder
        resumed = await self.store.get_run(run.id)
        self.assertEqual(resumed.status, "running")
        self.assertEqual(resumed.state_reason, "approval_resolved")


if __name__ == "__main__":
    unittest.main()

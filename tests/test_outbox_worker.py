from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.outbox import OutboxWorker
from nexapilot.store.sqlite import SQLiteStore


class OutboxWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_processes_claimed_event_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            event = await store.enqueue_outbox_event(
                idempotency_key="event-1",
                event_type="test.event",
                aggregate_type="test",
                aggregate_id="1",
                payload={"value": 1},
            )
            handled: list[str] = []

            async def handler(item):
                handled.append(item.id)

            worker = OutboxWorker(store=store, worker_id="worker-1", handlers={"test.event": handler})
            self.assertEqual(await worker.run_once(now_ms=100), 1)
            self.assertEqual(await worker.run_once(now_ms=101), 0)
            self.assertEqual(handled, [event.id])
            processed = await store.get_outbox_event(event.id)
            self.assertEqual(processed.status, "processed")
            self.assertEqual(processed.attempts, 1)

    async def test_worker_retries_then_dead_letters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            event = await store.enqueue_outbox_event(
                idempotency_key="event-1",
                event_type="test.event",
                aggregate_type="test",
                aggregate_id="1",
                payload={},
            )

            async def failing_handler(_item):
                raise RuntimeError("boom")

            worker = OutboxWorker(
                store=store,
                worker_id="worker-1",
                handlers={"test.event": failing_handler},
                max_attempts=3,
                retry_base_ms=100,
            )
            await worker.run_once(now_ms=1_000)
            first = await store.get_outbox_event(event.id)
            self.assertEqual(first.status, "pending")
            self.assertEqual(first.next_retry_at, 1_100)

            self.assertEqual(await worker.run_once(now_ms=1_099), 0)
            await worker.run_once(now_ms=1_100)
            await worker.run_once(now_ms=1_300)
            dead = await store.get_outbox_event(event.id)
            self.assertEqual(dead.status, "dead_letter")
            self.assertEqual(dead.attempts, 3)
            self.assertIn("RuntimeError: boom", dead.last_error or "")

    async def test_stale_processing_lease_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            event = await store.enqueue_outbox_event(
                idempotency_key="event-1",
                event_type="test.event",
                aggregate_type="test",
                aggregate_id="1",
                payload={},
            )
            claimed = await store.claim_outbox_events(
                worker_id="crashed-worker",
                limit=1,
                now_ms=1_000,
                lease_timeout_ms=500,
            )
            self.assertEqual([item.id for item in claimed], [event.id])

            handled: list[str] = []

            async def handler(item):
                handled.append(item.id)

            worker = OutboxWorker(
                store=store,
                worker_id="recovery-worker",
                handlers={"test.event": handler},
                lease_timeout_ms=500,
            )
            self.assertEqual(await worker.run_once(now_ms=1_999), 0)
            self.assertEqual(await worker.run_once(now_ms=2_000), 1)
            self.assertEqual(handled, [event.id])
            recovered = await store.get_outbox_event(event.id)
            self.assertEqual(recovered.status, "processed")
            self.assertEqual(recovered.attempts, 2)

    async def test_worker_does_not_claim_events_owned_by_another_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            own = await store.enqueue_outbox_event(
                idempotency_key="memory-event",
                event_type="run.completed",
                aggregate_type="run",
                aggregate_id="run-1",
                payload={},
            )
            foreign = await store.enqueue_outbox_event(
                idempotency_key="task-event",
                event_type="task.created",
                aggregate_type="goal",
                aggregate_id="goal-1",
                payload={},
            )
            handled: list[str] = []

            async def handler(item):
                handled.append(item.id)

            worker = OutboxWorker(
                store=store,
                worker_id="memory-worker",
                handlers={"run.completed": handler},
            )
            self.assertEqual(await worker.run_once(now_ms=1_000), 1)
            self.assertEqual(handled, [own.id])
            self.assertEqual((await store.get_outbox_event(own.id)).status, "processed")
            self.assertEqual((await store.get_outbox_event(foreign.id)).status, "pending")


if __name__ == "__main__":
    unittest.main()

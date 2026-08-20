from __future__ import annotations

import asyncio
from contextlib import suppress

from nexapilot.bus.bus import Bus, Event
from nexapilot.log import logger
from nexapilot.model import Run
from nexapilot.store.sqlite import SQLiteStore


TERMINAL_RUN_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"acquiring", "running", "cancelled", "failed", "interrupted"}),
    "acquiring": frozenset({"running", "waiting_retry", "cancelled", "failed", "interrupted"}),
    "running": frozenset(
        {
            "waiting_approval",
            "waiting_retry",
            "recovery_pending",
            "cancelling",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }
    ),
    "waiting_approval": frozenset(
        {"running", "cancelling", "cancelled", "failed", "interrupted"}
    ),
    "waiting_retry": frozenset(
        {"acquiring", "running", "cancelling", "cancelled", "failed", "interrupted"}
    ),
    "recovery_pending": frozenset(
        {"queued", "running", "failed", "cancelled", "interrupted"}
    ),
    "cancelling": frozenset({"cancelled", "interrupted"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}


def validate_run_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in RUN_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"illegal run transition: {current} -> {target}")


class DurableRunReconciler:
    """Converges abandoned persistent Runs to an auditable terminal state."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        bus: Bus,
        owner_id: str,
        lease_duration_ms: int,
        interval_ms: int,
    ) -> None:
        self._store = store
        self._bus = bus
        self._owner_id = owner_id
        self._lease_duration_ms = max(1_000, lease_duration_ms)
        self._interval_seconds = max(0.25, interval_ms / 1_000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> list[Run]:
        recovered = await self.reconcile_once()
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(), name="durable-run-reconciler"
            )
        return recovered

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                try:
                    await self.reconcile_once()
                except Exception:
                    logger.error(
                        "Durable Run reconciliation failed",
                        event="run.reconcile.error",
                        exc_info=True,
                    )

    async def reconcile_once(self) -> list[Run]:
        runs = await self._store.reconcile_abandoned_runs(
            owner_id=self._owner_id,
            stale_after_ms=self._lease_duration_ms,
        )
        for run in runs:
            logger.warning(
                "Abandoned Run was interrupted during reconciliation",
                event="run.reconciled",
                session_id=run.session_id,
                run_id=run.id,
                previous_status=(run.error or {}).get("previous_status"),
                error_code=run.error_code,
            )
            await self._publish_snapshot(run)
        return runs

    async def _publish_snapshot(self, run: Run) -> None:
        await self._bus.publish(
            Event(
                type="run.state.changed",
                properties={
                    "session_id": run.session_id,
                    "run_id": run.id,
                    "to": run.status,
                    "revision": run.revision,
                    "reason": run.finish_reason,
                    "error_code": run.error_code,
                },
            )
        )
        messages = await self._store.list_messages(run.session_id, run_id=run.id)
        for message in messages:
            await self._bus.publish(
                Event(
                    type="message.updated",
                    properties={
                        "session_id": run.session_id,
                        "info": message.info.model_dump(),
                    },
                )
            )
            latest_parts = {part.id: part for part in message.parts}
            for part in latest_parts.values():
                await self._bus.publish(
                    Event(
                        type="message.part.updated",
                        properties={
                            "session_id": run.session_id,
                            "part": part.model_dump(),
                        },
                    )
                )
        await self._bus.publish(
            Event(
                type="session.status",
                properties={"session_id": run.session_id, "status": "idle"},
            )
        )


__all__ = [
    "DurableRunReconciler",
    "RUN_TRANSITIONS",
    "TERMINAL_RUN_STATES",
    "validate_run_transition",
]

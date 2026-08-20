from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from nexapilot.bus.bus import Bus, Event
from nexapilot.hookdefs import Hook
from nexapilot.hooks import Hooker
from nexapilot.log import logger
from nexapilot.model import PermissionReply, PermissionRequest, PermissionRule
from nexapilot.permission.rules import evaluate_permission
from nexapilot.store.sqlite import SQLiteStore


class PermissionRejected(Exception):
    pass


class PermissionService:
    def __init__(self, bus: Bus, store: SQLiteStore, hooks: Hooker | None = None) -> None:
        self._bus = bus
        self._store = store
        self._hooks = hooks
        self._pending: dict[str, asyncio.Future[PermissionReply]] = {}
        self._pending_sessions: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def ask(
        self,
        *,
        session_id: str,
        ruleset: list[PermissionRule],
        permission: str,
        patterns: list[str],
        metadata: dict[str, Any],
        always: list[str],
        tool: dict[str, str] | None = None,
    ) -> None:
        approvals = await self._store.list_approvals(session_id)
        approved_rules = [PermissionRule.model_validate(r) for r in approvals]

        for p in patterns:
            # Persisted user approvals override the baseline catch-all rule
            # (normally ``* -> ask``). Permission evaluation is first-match-wins.
            d = evaluate_permission(permission, p, approved_rules + ruleset)
            if d.action == "allow":
                continue
            if d.action == "deny":
                raise PermissionRejected(f"permission denied: {permission} {p}")

            if self._hooks:
                out: dict[str, object] = {"status": "ask"}
                await self._hooks.trigger(
                    Hook.PermissionAsk,
                    {
                        "session_id": session_id,
                        "permission": permission,
                        "pattern": p,
                        "patterns": patterns,
                        "metadata": metadata,
                        "always": always,
                        "tool": tool,
                    },
                    out,
                )
                status = str(out.get("status") or "ask")
                if status == "allow":
                    continue
                if status == "deny":
                    raise PermissionRejected(f"permission denied: {permission} {p}")

            source = str(metadata.get("source", "") or "").strip()
            if source.startswith("channel:"):
                req = PermissionRequest(
                    id=str(uuid4()),
                    session_id=session_id,
                    permission=permission,
                    patterns=patterns,
                    metadata=metadata,
                    always=always,
                    tool=tool,
                )
                await self._store.create_permission_request(req)
                await self._bus.publish(Event(type="permission.asked", properties=req.model_dump()))
                await self._store.resolve_permission_request(req.id, "rejected")
                await self._bus.publish(
                    Event(
                        type="permission.replied",
                        properties={
                            "session_id": session_id,
                            "request_id": req.id,
                            "reply": "reject",
                            "reason": "channel_auto_reject",
                        },
                    ),
                )
                logger.warning(
                    "Permission ask auto-rejected for channel source",
                    event="permission.channel.auto_reject",
                    session_id=session_id,
                    source=source,
                    permission=permission,
                    pattern=p,
                )
                raise PermissionRejected(
                    f"permission requires interactive approval: {permission} {p}; channel source does not support approval flow"
                )

            req = PermissionRequest(
                id=str(uuid4()),
                session_id=session_id,
                permission=permission,
                patterns=patterns,
                metadata=metadata,
                always=always,
                tool=tool,
            )
            await self._store.create_permission_request(req)

            fut: asyncio.Future[PermissionReply] = asyncio.get_event_loop().create_future()
            async with self._lock:
                self._pending[req.id] = fut
                self._pending_sessions[req.id] = session_id

            assistant_message_id = str((tool or {}).get("message_id", "") or "")
            if assistant_message_id:
                await self._store.set_run_approval_state(
                    assistant_message_id=assistant_message_id,
                    waiting=True,
                )
            try:
                await self._bus.publish(
                    Event(type="permission.asked", properties=req.model_dump())
                )
                reply = await fut
            finally:
                async with self._lock:
                    current = self._pending.get(req.id)
                    if current is fut:
                        del self._pending[req.id]
                    self._pending_sessions.pop(req.id, None)
                if assistant_message_id:
                    await self._store.set_run_approval_state(
                        assistant_message_id=assistant_message_id,
                        waiting=False,
                    )

            if reply.reply == "reject":
                await self._store.resolve_permission_request(req.id, "rejected")
                await self._bus.publish(
                    Event(
                        type="permission.replied",
                        properties={
                            "session_id": session_id,
                            "request_id": req.id,
                            "reply": "reject",
                        },
                    ),
                )
                raise PermissionRejected(reply.message or "rejected")

            if reply.reply == "once":
                await self._store.resolve_permission_request(req.id, "once")
                await self._bus.publish(
                    Event(
                        type="permission.replied",
                        properties={"session_id": session_id, "request_id": req.id, "reply": "once"},
                    ),
                )
                return

            await self._store.resolve_permission_request(req.id, "always")
            for a in always:
                await self._store.add_approval(session_id, permission, a, "allow")
            await self._bus.publish(
                Event(
                    type="permission.replied",
                    properties={"session_id": session_id, "request_id": req.id, "reply": "always"},
                ),
            )
            return

    async def reply(self, request_id: str, reply: PermissionReply) -> None:
        async with self._lock:
            fut = self._pending.get(request_id)
            if not fut:
                return
            if fut.done():
                return
            fut.set_result(reply)

    async def cancel_session(
        self, session_id: str, *, reason: str = "user_cancelled"
    ) -> int:
        cancelled = 0
        async with self._lock:
            for request_id, pending_session_id in list(
                self._pending_sessions.items()
            ):
                if pending_session_id != session_id:
                    continue
                fut = self._pending.get(request_id)
                if fut is None or fut.done():
                    continue
                fut.set_result(PermissionReply(reply="reject", message=reason))
                cancelled += 1
        return cancelled

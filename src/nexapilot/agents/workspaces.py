from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from nexapilot.agents.types import AgentWorkspacePolicy
from nexapilot.bus.bus import Bus, Event
from nexapilot.model import Session
from nexapilot.store.sqlite import SQLiteStore


class AgentWorkspaceError(RuntimeError):
    pass


class AgentWorkspaceDirty(AgentWorkspaceError):
    pass


class GitWorktreeManager:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        bus: Bus,
        command_timeout_seconds: int = 30,
    ) -> None:
        self._store = store
        self._bus = bus
        self._timeout = command_timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def provision(
        self,
        *,
        parent_session: Session,
        child_session_id: str,
        policy: AgentWorkspacePolicy,
    ) -> dict[str, Any] | None:
        if policy.mode == "shared":
            return None
        if parent_session.runtime.backend != "local":
            raise AgentWorkspaceError(
                "git_worktree isolation is only available for the local runtime"
            )
        lock = await self._lock_for(child_session_id)
        async with lock:
            try:
                existing = await self._store.get_agent_workspace_for_session(
                    child_session_id
                )
            except KeyError:
                existing = None
            if existing is not None:
                if existing["status"] in {"ready", "retained"}:
                    return await self.inspect(existing["id"])
                raise AgentWorkspaceError(
                    "agent workspace cannot be reprovisioned from status "
                    f"{existing['status']}"
                )

            repository_root = await self._git(
                "-C", parent_session.worktree, "rev-parse", "--show-toplevel"
            )
            repository_root = str(Path(repository_root).resolve())
            base_commit = await self._git(
                "-C", parent_session.cwd, "rev-parse", "HEAD"
            )
            workspace_id = str(uuid4())
            token = child_session_id.replace("-", "").lower()[:16]
            repo_hash = hashlib.sha256(repository_root.encode()).hexdigest()[:10]
            worktree_path = str(
                (
                    Path(repository_root).parent
                    / ".nexa-worktrees"
                    / f"{Path(repository_root).name}-{repo_hash}"
                    / token
                ).resolve()
            )
            branch_name = f"nexapilot/{token}"
            now = self._now_ms()
            record = {
                "id": workspace_id,
                "child_session_id": child_session_id,
                "root_session_id": parent_session.root_session_id
                or parent_session.id,
                "repository_root": repository_root,
                "worktree_path": worktree_path,
                "branch_name": branch_name,
                "base_commit": base_commit,
                "head_commit": base_commit,
                "status": "provisioning",
                "cleanup_policy": policy.cleanup,
                "dirty": False,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "released_at": None,
            }
            await self._store.create_agent_workspace(record)
            await self._publish("agent.workspace.provisioning", record)
            try:
                await self._git(
                    "-C",
                    repository_root,
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    worktree_path,
                    base_commit,
                )
            except Exception as exc:
                failed = await self._store.update_agent_workspace(
                    workspace_id,
                    status="failed",
                    error=self._safe_error(exc),
                    updated_at=self._now_ms(),
                )
                await self._publish("agent.workspace.failed", failed)
                raise
            ready = await self._store.update_agent_workspace(
                workspace_id,
                status="ready",
                error=None,
                updated_at=self._now_ms(),
            )
            await self._publish("agent.workspace.ready", ready)
            return ready

    async def ensure_resumable(self, child_session_id: str) -> dict[str, Any]:
        record = await self._store.get_agent_workspace_for_session(child_session_id)
        if record["status"] not in {"ready", "retained"}:
            raise AgentWorkspaceError(
                f"agent workspace is not resumable: {record['status']}"
            )
        inspected = await self.inspect(record["id"])
        if inspected["status"] == "missing":
            raise AgentWorkspaceError("agent workspace directory is missing")
        return await self._store.update_agent_workspace(
            record["id"], status="ready", updated_at=self._now_ms()
        )

    async def retain(self, child_session_id: str) -> dict[str, Any] | None:
        try:
            record = await self._store.get_agent_workspace_for_session(
                child_session_id
            )
        except KeyError:
            return None
        lock = await self._lock_for(record["id"])
        async with lock:
            current = await self._store.get_agent_workspace(record["id"])
            if current["status"] in {"released", "missing"}:
                return current
            inspected = await self.inspect(record["id"])
            if inspected["status"] in {"released", "missing"}:
                return inspected
            retained = await self._store.update_agent_workspace(
                record["id"], status="retained", updated_at=self._now_ms()
            )
            await self._publish("agent.workspace.retained", retained)
            return retained

    async def inspect(self, workspace_id: str) -> dict[str, Any]:
        record = await self._store.get_agent_workspace(workspace_id)
        if record["status"] == "released":
            return record
        path = Path(record["worktree_path"])
        if not path.is_dir():
            missing = await self._store.update_agent_workspace(
                workspace_id,
                status="missing",
                error="worktree directory is missing",
                updated_at=self._now_ms(),
            )
            await self._publish("agent.workspace.missing", missing)
            return missing
        head_commit = await self._git("-C", str(path), "rev-parse", "HEAD")
        dirty = bool(await self._git("-C", str(path), "status", "--porcelain"))
        return await self._store.update_agent_workspace(
            workspace_id,
            head_commit=head_commit,
            dirty=dirty,
            error=None,
            updated_at=self._now_ms(),
        )

    async def release(
        self, workspace_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        lock = await self._lock_for(workspace_id)
        async with lock:
            record = await self._store.get_agent_workspace(workspace_id)
            if record["status"] == "released":
                return record
            inspected = await self.inspect(workspace_id)
            if inspected["status"] == "missing":
                return inspected
            if inspected["dirty"] and not force:
                raise AgentWorkspaceDirty(
                    "agent workspace has uncommitted changes; force is required"
                )
            releasing = await self._store.update_agent_workspace(
                workspace_id,
                status="releasing",
                updated_at=self._now_ms(),
            )
            await self._publish("agent.workspace.release.started", releasing)
            ahead = int(
                await self._git(
                    "-C",
                    inspected["repository_root"],
                    "rev-list",
                    "--count",
                    f"{inspected['base_commit']}..{inspected['branch_name']}",
                )
            )
            args = [
                "-C",
                inspected["repository_root"],
                "worktree",
                "remove",
            ]
            if force:
                args.append("--force")
            args.append(inspected["worktree_path"])
            try:
                await self._git(*args)
                if ahead == 0:
                    await self._git(
                        "-C",
                        inspected["repository_root"],
                        "branch",
                        "-D",
                        inspected["branch_name"],
                    )
            except Exception as exc:
                failed = await self._store.update_agent_workspace(
                    workspace_id,
                    status="failed",
                    error=self._safe_error(exc),
                    updated_at=self._now_ms(),
                )
                await self._publish("agent.workspace.failed", failed)
                raise
            released = await self._store.update_agent_workspace(
                workspace_id,
                status="released",
                dirty=False,
                error=None,
                updated_at=self._now_ms(),
                released_at=self._now_ms(),
            )
            released["branch_retained"] = ahead > 0
            await self._publish("agent.workspace.released", released)
            return released

    async def reconcile(self) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        for record in await self._store.list_agent_workspaces():
            if record["status"] == "provisioning":
                changed.append(
                    await self._store.update_agent_workspace(
                        record["id"],
                        status="failed",
                        error="provisioning was interrupted",
                        updated_at=self._now_ms(),
                    )
                )
            elif record["status"] in {"ready", "retained"} and not Path(
                record["worktree_path"]
            ).is_dir():
                changed.append(await self.inspect(record["id"]))
        return changed

    async def _lock_for(self, resource_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(resource_id, asyncio.Lock())

    async def _git(self, *args: str) -> str:
        def run() -> str:
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            try:
                completed = subprocess.run(
                    ["git", *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=self._timeout,
                    shell=False,
                    creationflags=creation_flags,
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentWorkspaceError("git command timed out") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[:2_000]
                raise AgentWorkspaceError(detail or "git command failed")
            return completed.stdout.strip()

        return await asyncio.to_thread(run)

    async def _publish(self, event_type: str, record: dict[str, Any]) -> None:
        await self._bus.publish(
            Event(
                type=event_type,
                properties={
                    "workspace_id": record["id"],
                    "child_session_id": record["child_session_id"],
                    "root_session_id": record["root_session_id"],
                    "status": record["status"],
                    "branch_name": record["branch_name"],
                },
            )
        )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return str(exc).strip().replace("\x00", "")[:2_000]

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1_000)

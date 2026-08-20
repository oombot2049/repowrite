from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from nexapilot.agents.registry import AgentRegistry
from nexapilot.agents.service import AgentService
from nexapilot.agents.types import AgentDefinition, AgentWorkspacePolicy
from nexapilot.agents.workspaces import (
    AgentWorkspaceDirty,
    AgentWorkspaceError,
    GitWorktreeManager,
)
from nexapilot.bus.bus import Bus
from nexapilot.model import PermissionRule, Session
from nexapilot.store.sqlite import SQLiteStore


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class FakeMemoryService:
    def __init__(self) -> None:
        self.worktrees: list[str] = []

    async def ensure_worktree(self, worktree: str):
        self.worktrees.append(worktree)
        return None


class GitWorktreeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "tests@nexapilot.local")
        git(self.repo, "config", "user.name", "NexaPilot Tests")
        (self.repo / "shared.txt").write_text("primary\n", encoding="utf-8")
        git(self.repo, "add", "shared.txt")
        git(self.repo, "commit", "-m", "initial")
        self.store = SQLiteStore(str(self.root / "workspaces.sqlite3"))
        await self.store.init()
        now = int(time.time() * 1_000)
        self.parent = Session(
            id="parent-session",
            title="Parent",
            worktree=str(self.repo),
            cwd=str(self.repo),
            created_at=now,
            updated_at=now,
            permission_rules=[
                PermissionRule(permission="*", pattern="*", action="allow")
            ],
            root_session_id="parent-session",
            project_id="project-1",
        )
        await self.store.create_session(self.parent)
        self.manager = GitWorktreeManager(store=self.store, bus=Bus())

    async def asyncTearDown(self) -> None:
        for record in await self.store.list_agent_workspaces():
            if record["status"] not in {"released", "missing"}:
                try:
                    await self.manager.release(record["id"], force=True)
                except Exception:
                    pass
        self.tmp.cleanup()

    async def test_parallel_worktrees_isolate_files_and_release_safely(self) -> None:
        policy = AgentWorkspacePolicy(mode="git_worktree")
        first = await self.manager.provision(
            parent_session=self.parent,
            child_session_id="11111111-1111-1111-1111-111111111111",
            policy=policy,
        )
        second = await self.manager.provision(
            parent_session=self.parent,
            child_session_id="22222222-2222-2222-2222-222222222222",
            policy=policy,
        )
        assert first is not None and second is not None
        first_path = Path(first["worktree_path"])
        second_path = Path(second["worktree_path"])
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first["branch_name"], second["branch_name"])

        (first_path / "shared.txt").write_text("first\n", encoding="utf-8")
        (second_path / "shared.txt").write_text("second\n", encoding="utf-8")
        self.assertEqual(
            (self.repo / "shared.txt").read_text(encoding="utf-8"), "primary\n"
        )
        self.assertTrue((await self.manager.inspect(first["id"]))["dirty"])
        self.assertTrue((await self.manager.inspect(second["id"]))["dirty"])

        with self.assertRaises(AgentWorkspaceDirty):
            await self.manager.release(first["id"])
        released_first = await self.manager.release(first["id"], force=True)
        self.assertEqual(released_first["status"], "released")
        self.assertFalse(first_path.exists())
        self.assertEqual(
            (await self.manager.release(first["id"], force=True))["status"],
            "released",
        )
        retained_after_release = await self.manager.retain(
            "11111111-1111-1111-1111-111111111111"
        )
        assert retained_after_release is not None
        self.assertEqual(retained_after_release["status"], "released")

        git(second_path, "add", "shared.txt")
        git(second_path, "commit", "-m", "isolated change")
        released_second = await self.manager.release(second["id"])
        self.assertTrue(released_second["branch_retained"])
        self.assertIn(
            second["branch_name"], git(self.repo, "branch", "--format=%(refname:short)")
        )

    async def test_non_git_parent_fails_before_workspace_record(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        parent = self.parent.model_copy(
            update={"worktree": str(plain), "cwd": str(plain)}
        )
        with self.assertRaises(AgentWorkspaceError):
            await self.manager.provision(
                parent_session=parent,
                child_session_id="33333333-3333-3333-3333-333333333333",
                policy=AgentWorkspacePolicy(mode="git_worktree"),
            )
        self.assertEqual(await self.store.list_agent_workspaces(), [])

    async def test_agent_service_binds_execution_and_memory_scopes(self) -> None:
        memory = FakeMemoryService()
        registry = AgentRegistry(
            (
                AgentDefinition(
                    name="primary", mode="primary", description="Primary"
                ),
                AgentDefinition(
                    name="implementer",
                    mode="subagent",
                    description="Implement",
                    workspace=AgentWorkspacePolicy(mode="git_worktree"),
                ),
            )
        )
        service = AgentService(
            cfg=SimpleNamespace(
                db_path=str(self.root / "workspaces.sqlite3"),
                openai=SimpleNamespace(model="test-model"),
            ),
            bus=Bus(),
            store=self.store,
            perm=SimpleNamespace(),
            llm=SimpleNamespace(),
            interrupt=SimpleNamespace(),
            hooks=None,
            memory_service=memory,
            daytona_manager=SimpleNamespace(),
            mcp_manager=SimpleNamespace(),
            registry=registry,
            workspace_manager=self.manager,
        )

        child = await service.create_child_session(
            parent_session=self.parent,
            agent_name="implementer",
            description="isolated change",
            parent_tool_call_id="tool-call",
        )

        self.assertNotEqual(Path(child.worktree), self.repo)
        self.assertEqual(child.cwd, child.worktree)
        self.assertEqual(Path(child.memory_worktree), self.repo)
        self.assertEqual(child.project_id, self.parent.project_id)
        self.assertEqual(memory.worktrees, [str(self.repo)])
        loaded_child = await self.store.get_session(child.id)
        self.assertEqual(Path(loaded_child.memory_worktree), self.repo)
        self.assertEqual(loaded_child.worktree, child.worktree)
        self.assertEqual(loaded_child.project_id, self.parent.project_id)
        record = await self.store.get_agent_workspace_for_session(child.id)
        released = await service.release_workspace(record["id"], force=True)
        self.assertEqual(released["status"], "released")

    async def test_reconcile_marks_interrupted_and_missing_records(self) -> None:
        now = int(time.time() * 1_000)
        await self.store.create_agent_workspace(
            {
                "id": "interrupted",
                "child_session_id": "child-interrupted",
                "root_session_id": self.parent.id,
                "repository_root": str(self.repo),
                "worktree_path": str(self.root / "missing-interrupted"),
                "branch_name": "nexapilot/interrupted",
                "base_commit": git(self.repo, "rev-parse", "HEAD"),
                "status": "provisioning",
                "cleanup_policy": "manual",
                "created_at": now,
                "updated_at": now,
            }
        )
        policy = AgentWorkspacePolicy(mode="git_worktree")
        ready = await self.manager.provision(
            parent_session=self.parent,
            child_session_id="44444444-4444-4444-4444-444444444444",
            policy=policy,
        )
        assert ready is not None
        await self.manager.release(ready["id"], force=True)
        await self.store.update_agent_workspace(
            ready["id"], status="retained", updated_at=now
        )

        changed = await self.manager.reconcile()

        states = {record["id"]: record["status"] for record in changed}
        self.assertEqual(states["interrupted"], "failed")
        self.assertEqual(states[ready["id"]], "missing")

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from nexapilot.artifacts import ArtifactIntegrityError, ArtifactStore
from nexapilot.model import ModelRef, PermissionRule, Session
from nexapilot.run_workspace import RunWorkspaceService
from nexapilot.store.sqlite import SQLiteStore


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class RunWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "tests@nexapilot.local")
        git(self.repo, "config", "user.name", "NexaPilot Tests")
        (self.repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-m", "initial")
        self.store = SQLiteStore(str(self.root / "store.sqlite3"))
        await self.store.init()
        now = int(time.time() * 1000)
        await self.store.create_session(
            Session(
                id="session-1",
                title="Review",
                worktree=str(self.repo),
                cwd=str(self.repo),
                created_at=now,
                updated_at=now,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
            )
        )
        self.run = await self.store.create_run(
            session_id="session-1",
            trigger_message_id=None,
            source="api",
            agent_name="primary",
            model=ModelRef(provider="openai-compatible", id="test-model"),
            now_ms=now,
        )
        self.artifacts = ArtifactStore(
            self.store,
            self.root / "artifacts",
            threshold_bytes=32,
            preview_head_chars=8,
            preview_tail_chars=4,
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_large_output_is_offloaded_and_integrity_checked(self) -> None:
        original = "0123456789" * 1_000
        materialized = await self.artifacts.materialize_tool_output(
            session_id="session-1",
            run_id=self.run.id,
            message_id="message-1",
            tool_call_id="call-1",
            tool_name="bash",
            output=original,
        )
        self.assertIsNotNone(materialized.artifact)
        artifact = materialized.artifact
        assert artifact is not None
        self.assertLess(len(materialized.output), len(original))
        self.assertIn(artifact.id, materialized.output)
        self.assertTrue(materialized.metadata["output_offloaded"])
        stored, content = await self.artifacts.read(artifact.id)
        self.assertEqual(content.decode(), original)
        self.assertEqual(stored.sha256, artifact.sha256)
        self.assertEqual((await self.store.list_artifacts(run_id=self.run.id))[0].id, artifact.id)

        (self.artifacts.root / artifact.storage_path).write_text("tampered", encoding="utf-8")
        with self.assertRaises(ArtifactIntegrityError):
            await self.artifacts.read(artifact.id)

    async def test_small_output_stays_inline(self) -> None:
        materialized = await self.artifacts.materialize_tool_output(
            session_id="session-1",
            run_id=self.run.id,
            message_id="message-1",
            tool_call_id="call-small",
            tool_name="read",
            output="small",
        )
        self.assertEqual(materialized.output, "small")
        self.assertIsNone(materialized.artifact)
        self.assertEqual(await self.store.list_artifacts(run_id=self.run.id), [])

    async def test_workspace_aggregates_diff_terminal_tests_and_artifacts(self) -> None:
        (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new evidence\n", encoding="utf-8")
        now = int(time.time() * 1000)
        await self.store.upsert_tool_operation(
            {
                "operation_id": "operation-1",
                "run_id": self.run.id,
                "session_id": "session-1",
                "message_id": "message-1",
                "tool_call_id": "call-1",
                "tool_name": "bash",
                "capability": "process.exec.shell",
                "canonical_target": "pytest -q",
                "executor_backend": "local_guarded",
                "isolation_level": "guarded_host",
                "status": "completed",
                "error_code": None,
                "input": {"command": "python -m pytest -q"},
                "result": {"title": "bash", "output": "2 passed", "metadata": {"returncode": 0}},
                "created_at": now,
                "finished_at": now + 25,
            }
        )
        artifact = await self.artifacts.put_bytes(
            session_id="session-1",
            run_id=self.run.id,
            message_id="message-1",
            tool_call_id="call-1",
            kind="report",
            name="report.txt",
            media_type="text/plain",
            content=b"evidence",
        )

        workspace = await RunWorkspaceService(self.store).build(self.run.id)
        self.assertTrue(workspace["changes"]["available"])
        self.assertEqual(
            {item["path"]: item["status"] for item in workspace["changes"]["files"]},
            {"tracked.txt": "modified", "new.txt": "untracked"},
        )
        self.assertIn("-before", workspace["changes"]["diff"])
        self.assertIn("+new evidence", workspace["changes"]["diff"])
        self.assertEqual(workspace["terminal"][0]["command"], "python -m pytest -q")
        self.assertEqual(workspace["tests"][0]["status"], "passed")
        self.assertEqual(workspace["artifacts"][0]["id"], artifact.id)
        self.assertNotIn("storage_path", workspace["artifacts"][0])

    async def test_non_git_workspace_degrades_without_hiding_run_evidence(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        now = int(time.time() * 1000)
        await self.store.create_session(
            Session(
                id="plain-session",
                title="Plain",
                worktree=str(plain),
                cwd=str(plain),
                created_at=now,
                updated_at=now,
                permission_rules=[],
            )
        )
        plain_run = await self.store.create_run(
            session_id="plain-session",
            trigger_message_id=None,
            source="api",
            agent_name="primary",
            model=ModelRef(provider="openai-compatible", id="test-model"),
        )
        workspace = await RunWorkspaceService(self.store).build(plain_run.id)
        self.assertFalse(workspace["changes"]["available"])
        self.assertIn("Git", workspace["changes"]["error"])

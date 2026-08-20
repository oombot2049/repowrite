from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from nexapilot.model import Artifact, Run, Session
from nexapilot.store.sqlite import SQLiteStore


_TEST_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:\S*python(?:\.exe)?\s+-m\s+)?pytest(?:\s|$)|"
    r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn)\s+(?:run\s+)?test(?:\s|$)|"
    r"(?:^|[;&|]\s*)(?:cargo|go)\s+test(?:\s|$)",
    re.IGNORECASE,
)


class RunWorkspaceService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        git_timeout_seconds: float = 10.0,
        max_diff_chars: int = 1_000_000,
    ) -> None:
        self._store = store
        self._git_timeout_seconds = git_timeout_seconds
        self._max_diff_chars = max_diff_chars

    async def build(self, run_id: str) -> dict[str, Any]:
        run = await self._store.get_run(run_id)
        session = await self._store.get_session(run.session_id)
        operations = await self._store.list_run_tool_operations(run_id)
        artifacts = await self._store.list_artifacts(run_id=run_id)
        terminal = [self._terminal_item(item) for item in operations if item["tool_name"] == "bash"]
        tests = [self._test_item(item) for item in terminal if _TEST_COMMAND.search(item["command"])]
        return {
            "run": run.model_dump(),
            "changes": await self._changes(session),
            "terminal": terminal,
            "tests": tests,
            "artifacts": [self.public_artifact(item) for item in artifacts],
        }

    @staticmethod
    def public_artifact(artifact: Artifact) -> dict[str, Any]:
        data = artifact.model_dump(exclude={"storage_path"})
        data["content_url"] = f"/artifacts/{artifact.id}/content"
        data["download_url"] = f"/artifacts/{artifact.id}/download"
        return data

    async def _changes(self, session: Session) -> dict[str, Any]:
        captured_at = int(time.time() * 1000)
        root = Path(session.worktree).resolve()
        if not root.is_dir():
            return self._unavailable_changes(captured_at, "session worktree is missing")
        try:
            inside = await self._git(root, "rev-parse", "--is-inside-work-tree")
            if inside[0] != 0 or inside[1].strip() != "true":
                return self._unavailable_changes(captured_at, "session worktree is not a Git repository")
            status_code, status_output, status_error = await self._git(
                root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
            )
            if status_code != 0:
                return self._unavailable_changes(captured_at, status_error or status_output)
            files = self._parse_status(status_output)
            diff_code, diff_output, diff_error = await self._git(
                root, "diff", "--no-ext-diff", "--no-color", "--binary", "HEAD", "--"
            )
            if diff_code != 0:
                return self._unavailable_changes(captured_at, diff_error or diff_output)
            diff_output += self._untracked_diff(root, files)
            truncated = len(diff_output) > self._max_diff_chars
            if truncated:
                diff_output = diff_output[: self._max_diff_chars] + "\n... [diff truncated]\n"
            return {
                "available": True,
                "scope": "live_worktree",
                "captured_at": captured_at,
                "files": files,
                "diff": diff_output,
                "truncated": truncated,
                "error": None,
            }
        except (asyncio.TimeoutError, OSError) as exc:
            return self._unavailable_changes(captured_at, str(exc))

    async def _git(self, root: Path, *args: str) -> tuple[int, str, str]:
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            f"safe.directory={root.as_posix()}",
            *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self._git_timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _parse_status(output: str) -> list[dict[str, str]]:
        entries = output.split("\x00")
        files: list[dict[str, str]] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            code = entry[:2]
            path = entry[3:]
            original_path = None
            if "R" in code or "C" in code:
                if index < len(entries):
                    original_path = entries[index]
                    index += 1
            status = RunWorkspaceService._status_name(code)
            item = {"path": path.replace("\\", "/"), "status": status, "code": code}
            if original_path:
                item["original_path"] = original_path.replace("\\", "/")
            files.append(item)
        return files

    @staticmethod
    def _status_name(code: str) -> str:
        if code == "??":
            return "untracked"
        if "R" in code:
            return "renamed"
        if "D" in code:
            return "deleted"
        if "A" in code:
            return "added"
        if "C" in code:
            return "copied"
        return "modified"

    def _untracked_diff(self, root: Path, files: list[dict[str, str]]) -> str:
        chunks: list[str] = []
        remaining = self._max_diff_chars
        for item in files:
            if item["status"] != "untracked" or remaining <= 0:
                continue
            path = (root / item["path"]).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file() or path.stat().st_size > remaining:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            lines = content.splitlines()
            chunk = (
                f"\ndiff --git a/{item['path']} b/{item['path']}\n"
                "new file mode 100644\n--- /dev/null\n"
                f"+++ b/{item['path']}\n@@ -0,0 +1,{len(lines)} @@\n"
                + "\n".join("+" + line for line in lines)
                + "\n"
            )
            chunks.append(chunk)
            remaining -= len(chunk)
        return "".join(chunks)

    @staticmethod
    def _terminal_item(operation: dict[str, Any]) -> dict[str, Any]:
        result = operation.get("result") or {}
        metadata = result.get("metadata") or {}
        return {
            "operation_id": operation["operation_id"],
            "command": str((operation.get("input") or {}).get("command") or ""),
            "workdir": str((operation.get("input") or {}).get("workdir") or metadata.get("workdir") or ""),
            "status": operation["status"],
            "error_code": operation.get("error_code"),
            "returncode": metadata.get("returncode"),
            "started_at": operation["created_at"],
            "finished_at": operation["finished_at"],
            "duration_ms": max(0, operation["finished_at"] - operation["created_at"]),
            "output": str(result.get("output") or ""),
            "artifact_id": metadata.get("artifact_id"),
        }

    @staticmethod
    def _test_item(terminal: dict[str, Any]) -> dict[str, Any]:
        return {
            **terminal,
            "status": "passed" if terminal.get("status") == "completed" and terminal.get("returncode") == 0 else "failed",
        }

    @staticmethod
    def _unavailable_changes(captured_at: int, error: str) -> dict[str, Any]:
        return {
            "available": False,
            "scope": "live_worktree",
            "captured_at": captured_at,
            "files": [],
            "diff": "",
            "truncated": False,
            "error": error,
        }

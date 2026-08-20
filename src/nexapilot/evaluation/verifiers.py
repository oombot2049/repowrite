from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from nexapilot.evaluation.models import CheckResult, EvalCheck, EvalObservation


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes evaluation workspace: {relative}") from exc
    return candidate


def _match_text(text: str, check: EvalCheck) -> bool:
    if check.pattern is not None:
        return re.search(check.pattern, text, flags=re.MULTILINE) is not None
    return (check.contains or "") in text


def _result(
    check: EvalCheck, passed: bool, detail: str, **evidence: Any
) -> CheckResult:
    return CheckResult(
        type=check.type,
        name=check.name or check.type.replace("_", " "),
        category=check.category,
        passed=passed,
        hard_gate=check.hard_gate,
        weight=check.weight,
        detail=detail,
        evidence=evidence,
    )


async def evaluate_check(check: EvalCheck, observation: EvalObservation) -> CheckResult:
    root = Path(observation.workspace).resolve()
    if check.type == "run_status":
        actual = str(observation.run.get("status", ""))
        expected = check.statuses or ["completed"]
        return _result(
            check,
            actual in expected,
            f"run status is {actual}; expected {expected}",
            actual=actual,
        )

    if check.type in {"assistant_contains", "assistant_not_contains"}:
        matched = _match_text(observation.assistant_text, check)
        passed = matched if check.type == "assistant_contains" else not matched
        return _result(
            check,
            passed,
            f"assistant text {'matched' if matched else 'did not match'} expectation",
        )

    if check.type in {
        "file_exists",
        "file_not_exists",
        "file_contains",
        "file_not_contains",
    }:
        try:
            path = _safe_path(root, check.path or "")
        except ValueError as exc:
            return _result(check, False, str(exc))
        exists = path.is_file()
        if check.type == "file_exists":
            return _result(
                check,
                exists,
                f"{check.path} {'exists' if exists else 'does not exist'}",
            )
        if check.type == "file_not_exists":
            return _result(
                check,
                not exists,
                f"{check.path} {'exists' if exists else 'does not exist'}",
            )
        if not exists:
            return _result(check, False, f"{check.path} does not exist")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _result(check, False, f"cannot read {check.path}: {exc}")
        matched = _match_text(content, check)
        passed = matched if check.type == "file_contains" else not matched
        return _result(
            check,
            passed,
            f"{check.path} {'matched' if matched else 'did not match'} expectation",
        )

    if check.type == "command":
        creationflags = (
            getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *(check.command or []),
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), check.timeout_ms / 1000
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return _result(
                    check, False, f"command timed out after {check.timeout_ms} ms"
                )
            code = int(process.returncode or 0)
            output = (stdout + stderr).decode("utf-8", errors="replace")[-20_000:]
            return _result(
                check,
                code == check.expected_exit_code,
                f"command exited {code}; expected {check.expected_exit_code}",
                returncode=code,
                output=output,
            )
        except OSError as exc:
            return _result(check, False, f"command could not start: {exc}")

    if check.type == "changed_files":
        changes = observation.workspace_view.get("changes") or {}
        paths = [str(item.get("path", "")) for item in changes.get("files", [])]
        missing = [
            pattern
            for pattern in check.required_paths
            if not any(fnmatch.fnmatch(path, pattern) for path in paths)
        ]
        forbidden = [
            path
            for path in paths
            if any(fnmatch.fnmatch(path, pattern) for pattern in check.forbidden_paths)
        ]
        outside = [
            path
            for path in paths
            if check.allowed_paths
            and not any(
                fnmatch.fnmatch(path, pattern) for pattern in check.allowed_paths
            )
        ]
        available = bool(changes.get("available"))
        passed = available and not missing and not forbidden and not outside
        return _result(
            check,
            passed,
            f"changed={paths}, missing={missing}, forbidden={forbidden}, outside_allowed={outside}",
            paths=paths,
            missing=missing,
            forbidden=forbidden,
            outside_allowed=outside,
        )

    tool_names = [str(item.get("tool_name", "")) for item in observation.operations]
    if check.type == "tool_called":
        passed = check.tool in tool_names
        return _result(
            check,
            passed,
            f"tool {check.tool} {'was' if passed else 'was not'} called",
            tools=tool_names,
        )
    if check.type == "tool_not_called":
        passed = check.tool not in tool_names
        return _result(
            check,
            passed,
            f"tool {check.tool} {'was not' if passed else 'was'} called",
            tools=tool_names,
        )
    if check.type == "no_tool_errors":
        errors = [
            {
                "tool": item.get("tool_name"),
                "error_code": item.get("error_code"),
                "status": item.get("status"),
            }
            for item in observation.operations
            if item.get("status") == "error" or item.get("error_code")
        ]
        return _result(
            check,
            not errors,
            f"tool errors: {errors}" if errors else "no tool errors",
            errors=errors,
        )
    if check.type == "artifact_exists":
        names = [str(item.get("name", "")) for item in observation.artifacts]
        passed = any(fnmatch.fnmatch(name, check.artifact_name or "") for name in names)
        return _result(
            check,
            passed,
            f"artifact {check.artifact_name} {'exists' if passed else 'is missing'}",
            artifacts=names,
        )
    return _result(check, False, f"unsupported check type: {check.type}")

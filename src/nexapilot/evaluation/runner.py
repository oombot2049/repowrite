from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import yaml

from nexapilot.cli.client import Client
from nexapilot.evaluation.models import (
    CaseResult,
    CheckResult,
    EvalCase,
    EvalDataset,
    EvalObservation,
    EvalReport,
    EvalSummary,
)
from nexapilot.evaluation.verifiers import evaluate_check

logger = logging.getLogger(__name__)


class EvalExecutor(Protocol):
    async def execute(self, case: EvalCase, workspace: Path) -> EvalObservation: ...

    async def cleanup(self, observation: EvalObservation) -> None: ...


class APIEvalExecutor:
    """Exercise the same HTTP entrypoints used by the console and CLI."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    async def ping(self) -> bool:
        return await Client(self._base_url).ping()

    @staticmethod
    def _permission_rules(case: EvalCase) -> list[dict[str, str]] | None:
        if case.permission_rules is not None:
            return [rule.model_dump() for rule in case.permission_rules]
        if case.permission_mode == "default":
            return None
        return [{"permission": "*", "pattern": "*", "action": case.permission_mode}]

    async def execute(self, case: EvalCase, workspace: Path) -> EvalObservation:
        client = Client(self._base_url, timeout=case.timeout_seconds)
        payload: dict[str, object] = {
            "worktree": str(workspace),
            "cwd": str(workspace),
            "title": f"eval: {case.id}",
            "runtime": {"backend": "local"},
        }
        rules = self._permission_rules(case)
        if rules is not None:
            payload["permission_rules"] = rules
        session_id: str | None = None
        try:
            session = await client.post("/sessions", json=payload)
            session_id = str(session["id"])
            started = time.perf_counter()
            await client.post(
                f"/sessions/{session_id}/messages", json={"text": case.prompt}
            )
            run_result = await client.post(f"/sessions/{session_id}/run")
            duration_ms = round((time.perf_counter() - started) * 1000)
            run_id = str(run_result["run_id"])
            (
                run,
                messages,
                operations,
                workspace_view,
                artifacts,
                llm,
            ) = await asyncio.gather(
                client.get(f"/runs/{run_id}"),
                client.get(f"/runs/{run_id}/messages"),
                client.get(f"/runs/{run_id}/operations"),
                client.get(f"/runs/{run_id}/workspace"),
                client.get(f"/runs/{run_id}/artifacts"),
                client.get(f"/runs/{run_id}/llm-calls"),
            )
        except BaseException:
            if session_id is not None:
                try:
                    await client.delete(f"/sessions/{session_id}")
                except Exception as cleanup_error:  # noqa: BLE001 - preserve original error
                    logger.warning(
                        "failed to clean incomplete evaluation session %s: %s",
                        session_id,
                        cleanup_error,
                    )
            raise
        assert session_id is not None
        assistant_text = "\n".join(
            str(part.get("text", ""))
            for message in messages
            if (message.get("info") or {}).get("role") == "assistant"
            for part in message.get("parts", [])
            if part.get("type") == "text"
        )
        return EvalObservation(
            session_id=session_id,
            run_id=run_id,
            workspace=str(workspace),
            run=run,
            messages=messages,
            operations=operations.get("operations", []),
            workspace_view=workspace_view,
            artifacts=artifacts.get("artifacts", []),
            llm_calls=llm.get("calls", []),
            llm_totals=llm.get("totals", {}),
            assistant_text=assistant_text,
            duration_ms=duration_ms,
        )

    async def cleanup(self, observation: EvalObservation) -> None:
        await Client(self._base_url).delete(f"/sessions/{observation.session_id}")


def load_dataset(path: Path) -> EvalDataset:
    raw_text = path.read_text(encoding="utf-8")
    raw = (
        yaml.safe_load(raw_text)
        if path.suffix.lower() in {".yaml", ".yml"}
        else json.loads(raw_text)
    )
    return EvalDataset.model_validate(raw)


def _run_git(root: Path, *args: str) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=creationflags,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def prepare_workspace(case: EvalCase, dataset_path: Path, workspaces_dir: Path) -> Path:
    workspace = workspaces_dir / f"{case.id}-{uuid4().hex[:8]}"
    fixture = None
    if case.fixture:
        candidate = Path(case.fixture)
        fixture = (
            candidate.resolve()
            if candidate.is_absolute()
            else (dataset_path.parent / candidate).resolve()
        )
        if not fixture.is_dir():
            raise ValueError(f"fixture directory does not exist: {fixture}")
        shutil.copytree(
            fixture,
            workspace,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
    else:
        workspace.mkdir(parents=True)
    _run_git(workspace, "init", "--quiet")
    _run_git(workspace, "config", "user.email", "eval@nexapilot.local")
    _run_git(workspace, "config", "user.name", "NexaPilot Eval")
    _run_git(workspace, "add", "-A")
    _run_git(
        workspace, "commit", "--allow-empty", "--quiet", "-m", "evaluation baseline"
    )
    return workspace


def _remove_workspace(workspace: Path) -> None:
    def make_writable(_function, path: str, _error) -> None:
        os.chmod(path, 0o700)
        Path(path).unlink(missing_ok=True) if Path(path).is_file() else os.rmdir(path)

    shutil.rmtree(workspace, onerror=make_writable)


def _budget_results(case: EvalCase, observation: EvalObservation) -> list[CheckResult]:
    totals = observation.llm_totals
    values = {
        "duration_ms": observation.duration_ms,
        "model_calls": int(totals.get("calls", 0)),
        "tool_calls": int(
            observation.run.get("tool_call_count", len(observation.operations))
        ),
        "total_tokens": int(totals.get("input_tokens", 0))
        + int(totals.get("output_tokens", 0)),
        "cost_microusd": int(totals.get("cost_microusd", 0)),
    }
    limits = {
        "duration_ms": case.budget.max_duration_ms,
        "model_calls": case.budget.max_model_calls,
        "tool_calls": case.budget.max_tool_calls,
        "total_tokens": case.budget.max_total_tokens,
        "cost_microusd": case.budget.max_cost_microusd,
    }
    results: list[CheckResult] = []
    for metric, limit in limits.items():
        if limit is None:
            continue
        actual = values[metric]
        results.append(
            CheckResult(
                type="budget",
                name=f"budget: {metric}",
                category="efficiency",
                passed=actual <= limit,
                hard_gate=False,
                weight=1.0,
                detail=f"{metric}={actual}, limit={limit}",
                evidence={"actual": actual, "limit": limit},
            )
        )
    return results


def _score(checks: list[CheckResult]) -> float:
    total = sum(item.weight for item in checks)
    return (
        round(sum(item.weight for item in checks if item.passed) / total, 4)
        if total
        else 1.0
    )


class AgentEvalRunner:
    def __init__(
        self, executor: EvalExecutor, *, keep_workspaces: bool = False
    ) -> None:
        self._executor = executor
        self._keep_workspaces = keep_workspaces

    async def run(
        self, dataset: EvalDataset, *, dataset_path: Path, output_dir: Path
    ) -> EvalReport:
        started_at = datetime.now(UTC)
        workspaces_dir = output_dir / "workspaces"
        workspaces_dir.mkdir(parents=True, exist_ok=True)
        results: list[CaseResult] = []
        for case in dataset.cases:
            workspace: Path | None = None
            observation: EvalObservation | None = None
            try:
                workspace = prepare_workspace(case, dataset_path, workspaces_dir)
                observation = await asyncio.wait_for(
                    self._executor.execute(case, workspace),
                    timeout=case.timeout_seconds + 10,
                )
                checks = [
                    await evaluate_check(check, observation) for check in case.checks
                ]
                checks.extend(_budget_results(case, observation))
                score = _score(checks)
                hard_failure = any(
                    item.hard_gate and not item.passed for item in checks
                )
                passed = not hard_failure and score >= case.min_score
                results.append(
                    CaseResult(
                        id=case.id,
                        name=case.name or case.id,
                        passed=passed,
                        score=score,
                        min_score=case.min_score,
                        checks=checks,
                        observation=observation,
                        workspace_preserved=self._keep_workspaces or not passed,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one broken case must not abort the suite
                results.append(
                    CaseResult(
                        id=case.id,
                        name=case.name or case.id,
                        passed=False,
                        score=0.0,
                        min_score=case.min_score,
                        checks=[],
                        observation=observation,
                        error=f"{type(exc).__name__}: {exc}",
                        workspace_preserved=True,
                    )
                )
            finally:
                if observation is not None:
                    try:
                        await self._executor.cleanup(observation)
                    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                        logger.warning(
                            "failed to clean evaluation session %s: %s",
                            observation.session_id,
                            exc,
                        )
                preserve = self._keep_workspaces or not results[-1].passed
                if workspace is not None and not preserve:
                    _remove_workspace(workspace)
        report = self._report(dataset, dataset_path, started_at, results)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        return report

    @staticmethod
    def _report(
        dataset: EvalDataset,
        dataset_path: Path,
        started_at: datetime,
        cases: list[CaseResult],
    ) -> EvalReport:
        observations = [
            case.observation for case in cases if case.observation is not None
        ]
        total_tokens = sum(
            int(obs.llm_totals.get("input_tokens", 0))
            + int(obs.llm_totals.get("output_tokens", 0))
            for obs in observations
        )
        hard_gate_failures = sum(
            1
            for case in cases
            for check in case.checks
            if check.hard_gate and not check.passed
        )
        safety_violations = sum(
            1
            for case in cases
            for check in case.checks
            if check.category == "safety" and not check.passed
        )
        passed = sum(case.passed for case in cases)
        count = len(cases)
        return EvalReport(
            dataset_name=dataset.name,
            dataset_path=str(dataset_path.resolve()),
            started_at=started_at.isoformat(),
            completed_at=datetime.now(UTC).isoformat(),
            summary=EvalSummary(
                cases=count,
                passed=passed,
                failed=count - passed,
                task_success_rate=round(passed / count, 4) if count else 1.0,
                mean_score=round(sum(case.score for case in cases) / count, 4)
                if count
                else 1.0,
                hard_gate_failures=hard_gate_failures,
                safety_violations=safety_violations,
                mean_duration_ms=round(
                    sum(obs.duration_ms for obs in observations) / len(observations), 2
                )
                if observations
                else 0.0,
                total_model_calls=sum(
                    int(obs.llm_totals.get("calls", 0)) for obs in observations
                ),
                total_tool_calls=sum(
                    int(obs.run.get("tool_call_count", len(obs.operations)))
                    for obs in observations
                ),
                total_tokens=total_tokens,
                total_cost_microusd=sum(
                    int(obs.llm_totals.get("cost_microusd", 0)) for obs in observations
                ),
            ),
            cases=cases,
        )

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from nexapilot.cli.app import app
from nexapilot.evaluation.models import (
    EvalCase,
    EvalCheck,
    EvalDataset,
    EvalObservation,
)
from nexapilot.evaluation.reporting import compare_baseline, markdown_report
from nexapilot.evaluation.runner import AgentEvalRunner, load_dataset
from nexapilot.evaluation.verifiers import evaluate_check


def observation(workspace: Path) -> EvalObservation:
    return EvalObservation(
        session_id="session-1",
        run_id="run-1",
        workspace=str(workspace),
        run={"status": "completed", "tool_call_count": 1},
        messages=[],
        operations=[{"tool_name": "write", "status": "completed", "error_code": None}],
        workspace_view={
            "changes": {
                "available": True,
                "files": [{"path": "answer.txt", "status": "added"}],
            }
        },
        artifacts=[{"name": "answer.txt"}],
        llm_totals={
            "calls": 2,
            "input_tokens": 20,
            "output_tokens": 10,
            "cost_microusd": 4,
        },
        assistant_text="Implemented the fix and ran tests.",
        duration_ms=50,
    )


class FakeExecutor:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    async def execute(self, _case: EvalCase, workspace: Path) -> EvalObservation:
        (workspace / "answer.txt").write_text("correct", encoding="utf-8")
        return observation(workspace)

    async def cleanup(self, result: EvalObservation) -> None:
        self.cleaned.append(result.session_id)


class FakeAPIExecutor(FakeExecutor):
    def __init__(self, _base_url: str) -> None:
        super().__init__()

    async def ping(self) -> bool:
        return True


class EvalVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_real_outputs_and_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "answer.txt").write_text("correct", encoding="utf-8")
            obs = observation(root)
            checks = [
                {"type": "run_status", "statuses": ["completed"]},
                {"type": "assistant_contains", "contains": "tests"},
                {"type": "file_contains", "path": "answer.txt", "contains": "correct"},
                {"type": "tool_called", "tool": "write"},
                {"type": "no_tool_errors"},
                {"type": "artifact_exists", "artifact_name": "*.txt"},
                {
                    "type": "changed_files",
                    "allowed_paths": ["*.txt"],
                    "required_paths": ["answer.txt"],
                },
                {
                    "type": "command",
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('answer.txt').exists()",
                    ],
                },
            ]
            results = [
                await evaluate_check(EvalCheck.model_validate(item), obs)
                for item in checks
            ]
            self.assertTrue(all(item.passed for item in results), results)

    async def test_rejects_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = await evaluate_check(
                EvalCheck(
                    type="file_exists",
                    path="../secret.txt",
                    category="safety",
                    hard_gate=True,
                ),
                observation(Path(tmp)),
            )
            self.assertFalse(result.passed)
            self.assertIn("escapes", result.detail)


class AgentEvalRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_scores_budgets_and_writes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "seed.txt").write_text("seed", encoding="utf-8")
            dataset_path = root / "dataset.json"
            dataset_path.write_text("{}", encoding="utf-8")
            dataset = EvalDataset.model_validate(
                {
                    "version": 1,
                    "name": "unit",
                    "cases": [
                        {
                            "id": "case-1",
                            "prompt": "do it",
                            "fixture": "fixture",
                            "checks": [
                                {
                                    "type": "file_contains",
                                    "path": "answer.txt",
                                    "contains": "correct",
                                    "hard_gate": True,
                                },
                                {"type": "no_tool_errors"},
                            ],
                            "budget": {"max_model_calls": 2, "max_tool_calls": 1},
                        }
                    ],
                }
            )
            executor = FakeExecutor()
            output_dir = root / "results"
            report = await AgentEvalRunner(executor).run(
                dataset, dataset_path=dataset_path, output_dir=output_dir
            )

            self.assertEqual(report.summary.passed, 1)
            self.assertEqual(report.summary.total_tokens, 30)
            self.assertEqual(executor.cleaned, ["session-1"])
            self.assertTrue((output_dir / "report.json").is_file())
            self.assertFalse(Path(report.cases[0].observation.workspace).exists())

    async def test_failed_case_preserves_workspace_for_debugging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = EvalDataset.model_validate(
                {
                    "version": 1,
                    "name": "failure",
                    "cases": [
                        {
                            "id": "case-1",
                            "prompt": "do it",
                            "checks": [
                                {
                                    "type": "file_exists",
                                    "path": "missing.txt",
                                    "hard_gate": True,
                                }
                            ],
                        }
                    ],
                }
            )
            report = await AgentEvalRunner(FakeExecutor()).run(
                dataset, dataset_path=root / "dataset.json", output_dir=root / "results"
            )
            self.assertFalse(report.cases[0].passed)
            self.assertTrue(Path(report.cases[0].observation.workspace).is_dir())


class EvalReportingTests(unittest.TestCase):
    def test_baseline_detects_quality_and_safety_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = EvalDataset.model_validate(
                {
                    "version": 1,
                    "name": "unit",
                    "cases": [
                        {"id": "x", "prompt": "x", "checks": [{"type": "run_status"}]}
                    ],
                }
            )

            async def build():
                return await AgentEvalRunner(FakeExecutor()).run(
                    dataset,
                    dataset_path=root / "dataset.json",
                    output_dir=root / "results",
                )

            report = asyncio.run(build())
            baseline = root / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {"summary": {"task_success_rate": 1.0, "safety_violations": 0}}
                ),
                encoding="utf-8",
            )
            compared = compare_baseline(report, baseline)
            self.assertEqual(compared.regressions, [])
            self.assertIn("Agent Evaluation", markdown_report(compared))


class EvalCliTests(unittest.TestCase):
    def test_validate_is_offline_and_reports_dataset_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "eval.json"
            dataset.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "name": "smoke",
                        "cases": [
                            {
                                "id": "one",
                                "prompt": "answer",
                                "checks": [{"type": "run_status"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                app, ["--json", "eval", "validate", "--dataset", str(dataset)]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(json.loads(result.stdout)["cases"], 1)

    def test_run_executes_runner_and_writes_both_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "eval.json"
            dataset.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "name": "cli-smoke",
                        "cases": [
                            {
                                "id": "one",
                                "prompt": "write answer",
                                "checks": [
                                    {
                                        "type": "file_contains",
                                        "path": "answer.txt",
                                        "contains": "correct",
                                        "hard_gate": True,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "reports"
            with patch("nexapilot.cli.commands.eval.APIEvalExecutor", FakeAPIExecutor):
                result = CliRunner().invoke(
                    app,
                    [
                        "--json",
                        "eval",
                        "run",
                        "--dataset",
                        str(dataset),
                        "--output-dir",
                        str(output_dir),
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(json.loads(result.stdout)["summary"]["passed"], 1)
            self.assertTrue((output_dir / "report.json").is_file())
            self.assertTrue((output_dir / "report.md").is_file())


class EvalDatasetTests(unittest.TestCase):
    def test_loads_yaml_and_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "eval.yaml"
            dataset.write_text(
                "version: 1\nname: sample\ncases:\n  - id: one\n    prompt: answer\n    checks:\n      - type: run_status\n",
                encoding="utf-8",
            )
            self.assertEqual(load_dataset(dataset).cases[0].id, "one")
            with self.assertRaises(ValueError):
                EvalDataset.model_validate(
                    {
                        "version": 1,
                        "name": "bad",
                        "cases": [
                            {
                                "id": "same",
                                "prompt": "a",
                                "checks": [{"type": "run_status"}],
                            },
                            {
                                "id": "same",
                                "prompt": "b",
                                "checks": [{"type": "run_status"}],
                            },
                        ],
                    }
                )

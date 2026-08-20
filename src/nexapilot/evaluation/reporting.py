from __future__ import annotations

import json
from pathlib import Path

from nexapilot.evaluation.models import EvalReport


def compare_baseline(report: EvalReport, baseline_path: Path) -> EvalReport:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    summary = baseline.get("summary", {})
    report.baseline = {"path": str(baseline_path.resolve()), "summary": summary}
    if report.summary.task_success_rate < float(summary.get("task_success_rate", 0)):
        report.regressions.append(
            f"task_success_rate decreased from {summary.get('task_success_rate')} to {report.summary.task_success_rate}"
        )
    if report.summary.safety_violations > int(summary.get("safety_violations", 0)):
        report.regressions.append(
            f"safety_violations increased from {summary.get('safety_violations', 0)} to {report.summary.safety_violations}"
        )
    return report


def markdown_report(report: EvalReport) -> str:
    summary = report.summary
    lines = [
        f"# Agent Evaluation: {report.dataset_name}",
        "",
        f"- Result: **{'PASS' if summary.failed == 0 and not report.regressions else 'FAIL'}**",
        f"- Cases: {summary.passed}/{summary.cases} passed",
        f"- Task success rate: {summary.task_success_rate:.1%}",
        f"- Mean score: {summary.mean_score:.1%}",
        f"- Safety violations: {summary.safety_violations}",
        f"- Model/tool calls: {summary.total_model_calls}/{summary.total_tool_calls}",
        f"- Tokens/cost: {summary.total_tokens}/{summary.total_cost_microusd} micro-USD",
        "",
        "## Cases",
        "",
        "| Case | Result | Score | Failed checks |",
        "|---|---:|---:|---|",
    ]
    for case in report.cases:
        failed = (
            ", ".join(check.name for check in case.checks if not check.passed)
            or case.error
            or "-"
        )
        lines.append(
            f"| {case.id} | {'PASS' if case.passed else 'FAIL'} | {case.score:.1%} | {failed} |"
        )
    if report.regressions:
        lines.extend(
            ["", "## Regressions", "", *[f"- {item}" for item in report.regressions]]
        )
    lines.extend(["", "## Check details", ""])
    for case in report.cases:
        lines.append(f"### {case.id}")
        lines.append("")
        if case.error:
            lines.append(f"Execution error: `{case.error}`")
            lines.append("")
        for check in case.checks:
            lines.append(
                f"- {'PASS' if check.passed else 'FAIL'} `{check.name}`: {check.detail}"
            )
        lines.append("")
    return "\n".join(lines)

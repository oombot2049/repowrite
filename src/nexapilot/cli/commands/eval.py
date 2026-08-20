"""nexa eval — repeatable end-to-end Agent quality evaluation."""

from __future__ import annotations

# ruff: noqa: B008
import asyncio
from pathlib import Path

import typer

from nexapilot.cli.output import Output
from nexapilot.evaluation.reporting import compare_baseline, markdown_report
from nexapilot.evaluation.runner import AgentEvalRunner, APIEvalExecutor, load_dataset

eval_app = typer.Typer(
    no_args_is_help=True, context_settings={"allow_interspersed_args": False}
)


@eval_app.callback()
def _eval_callback(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    from nexapilot.cli.app import _apply_global_opts

    _apply_global_opts(json=json_output)


@eval_app.command("validate")
def validate_dataset(
    dataset: Path = typer.Option(
        ..., "--dataset", exists=True, dir_okay=False, readable=True
    ),
):
    """Validate an evaluation dataset without running the Agent."""
    from nexapilot.cli.app import state

    output = Output(json_mode=state.json_mode)
    try:
        parsed = load_dataset(dataset.resolve())
        output.data(
            {
                "valid": True,
                "name": parsed.name,
                "version": parsed.version,
                "cases": len(parsed.cases),
            },
            title="Agent Eval Dataset",
        )
    except Exception as exc:
        output.error(f"Invalid evaluation dataset: {exc}")
        raise typer.Exit(2) from exc


@eval_app.command("run")
def run_evaluation(
    dataset: Path = typer.Option(
        ..., "--dataset", exists=True, dir_okay=False, readable=True
    ),
    output_dir: Path = typer.Option(
        Path("eval-results"), "--output-dir", file_okay=False
    ),
    baseline: Path | None = typer.Option(
        None, "--baseline", exists=True, dir_okay=False, readable=True
    ),
    keep_workspaces: bool = typer.Option(
        False, "--keep-workspaces/--cleanup-workspaces"
    ),
    fail_on_regression: bool = typer.Option(
        True, "--fail-on-regression/--allow-regression"
    ),
):
    """Run the Agent against an isolated dataset and emit JSON/Markdown evidence."""
    from nexapilot.cli.app import state

    output = Output(json_mode=state.json_mode)
    dataset_path = dataset.resolve()
    report_dir = output_dir.resolve()

    async def execute():
        executor = APIEvalExecutor(state.base_url)
        if not await executor.ping():
            raise RuntimeError(
                f"Cannot reach server at {state.base_url}; start it with `nexa serve`"
            )
        parsed = load_dataset(dataset_path)
        return await AgentEvalRunner(executor, keep_workspaces=keep_workspaces).run(
            parsed,
            dataset_path=dataset_path,
            output_dir=report_dir,
        )

    try:
        report = asyncio.run(execute())
        if baseline is not None:
            report = compare_baseline(report, baseline.resolve())
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (report_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
        output.data(report.model_dump(mode="json"), title="Agent Evaluation")
        output.info(
            f"Reports: {report_dir / 'report.json'} and {report_dir / 'report.md'}"
        )
        failed = report.summary.failed > 0 or (
            fail_on_regression and bool(report.regressions)
        )
        if failed:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        output.error(f"Evaluation failed: {exc}")
        raise typer.Exit(2) from exc

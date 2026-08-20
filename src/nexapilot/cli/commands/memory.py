"""nexa memory — governance and offline retrieval evaluation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from pydantic import TypeAdapter

from nexapilot.cli.output import Output
from nexapilot.memory.eval import MemoryEvalCase, MemoryEvaluator
from nexapilot.store.sqlite import SQLiteStore


memory_app = typer.Typer(no_args_is_help=True, context_settings={"allow_interspersed_args": False})


@memory_app.callback()
def _memory_callback(json_output: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    from nexapilot.cli.app import _apply_global_opts

    _apply_global_opts(json=json_output)


@memory_app.command("eval")
def memory_eval(
    dataset: str = typer.Option(..., "--dataset", help="JSON evaluation dataset."),
    db_path: str = typer.Option(..., "--db-path", help="NexaPilot SQLite database."),
    min_recall: float = typer.Option(0.8, "--min-recall"),
):
    """Run deterministic Semantic/Episodic retrieval evaluation."""
    from nexapilot.cli.app import state

    output = Output(json_mode=state.json_mode)
    try:
        raw = json.loads(Path(dataset).read_text(encoding="utf-8"))
        case_payload = raw.get("cases", []) if isinstance(raw, dict) else raw
        cases = TypeAdapter(list[MemoryEvalCase]).validate_python(case_payload)

        async def run():
            store = SQLiteStore(db_path)
            await store.init()
            return await MemoryEvaluator(store=store).evaluate(cases)

        report = asyncio.run(run())
        output.data(report, title="Memory Eval")
        summary = report["summary"]
        if summary["mean_recall_at_k"] < min_recall or summary["forbidden_hits"] > 0:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        output.error(str(exc))
        raise typer.Exit(1) from exc

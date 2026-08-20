from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from nexapilot.cli.app import app
from nexapilot.memory import MemoryEvalCase, MemoryEvaluator
from nexapilot.model import SemanticMemory
from nexapilot.store.sqlite import SQLiteStore


class MemoryEvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_recall_precision_leaks_and_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(str(Path(tmp) / "test.sqlite3"))
            await store.init()
            for memory_id, value in [
                ("expected", "每个需求独立提交"),
                ("forbidden", "生产密钥不应召回"),
            ]:
                await store.activate_semantic_memory(
                    SemanticMemory(
                        id=memory_id,
                        namespace="project",
                        workspace="workspace-a",
                        memory_type="preference",
                        subject="user",
                        predicate=memory_id,
                        value=value,
                        status="active",
                        confidence=0.9,
                        importance=0.8,
                        source_session_id="session-1",
                        source_message_ids=["message-1"],
                        content_hash=f"hash-{memory_id}",
                        extractor_version="test",
                        created_at=1,
                        updated_at=1,
                    )
                )
            report = await MemoryEvaluator(store=store).evaluate(
                [
                    MemoryEvalCase(
                        name="workflow preference",
                        memory_type="semantic",
                        workspace="workspace-a",
                        query="需求独立",
                        expected_ids=["expected"],
                        forbidden_ids=["forbidden"],
                        top_k=3,
                    )
                ]
            )

            summary = report["summary"]
            self.assertEqual(summary["hit_rate"], 1.0)
            self.assertEqual(summary["mean_recall_at_k"], 1.0)
            self.assertEqual(summary["forbidden_hits"], 0)
            self.assertGreaterEqual(summary["mean_latency_ms"], 0)


class MemoryEvalCliTests(unittest.TestCase):
    def test_cli_accepts_dataset_and_emits_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "eval.json"
            dataset.write_text(json.dumps({"cases": []}), encoding="utf-8")
            result = CliRunner().invoke(
                app,
                [
                    "--json",
                    "memory",
                    "eval",
                    "--dataset",
                    str(dataset),
                    "--db-path",
                    str(Path(tmp) / "test.sqlite3"),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["cases"], 0)


if __name__ == "__main__":
    unittest.main()

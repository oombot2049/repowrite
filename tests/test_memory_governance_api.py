from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from nexapilot.model import SemanticMemory


class MemoryGovernanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        worktree = root / "worktree"
        worktree.mkdir()
        config_path = root / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "openai": {
                        "base_url": "http://local.test/v1",
                        "api_key": "test-key",
                        "model": "test-model",
                    },
                    "langfuse": {"enabled": False},
                    "logging": {"console": False, "file": False},
                    "db_path": str(root / "test.sqlite3"),
                    "default_worktree": str(worktree),
                    "memory": {
                        "sync_interval_seconds": 3600,
                        "core": {"enabled": True, "max_tokens": 500},
                    },
                }
            ),
            encoding="utf-8",
        )
        import nexapilot.config as config_module

        cls._original_paths = config_module.GLOBAL_CONFIG_PATHS
        config_module.GLOBAL_CONFIG_PATHS = (str(config_path),)
        sys.modules.pop("nexapilot.api.app", None)
        cls.api = importlib.import_module("nexapilot.api.app")
        cls.client = TestClient(cls.api.app)
        cls.client.__enter__()
        cls.workspace = str(worktree.resolve())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        import nexapilot.config as config_module

        config_module.GLOBAL_CONFIG_PATHS = cls._original_paths
        cls._tmp.cleanup()

    def test_list_forget_and_core_rebuild(self) -> None:
        value = "one commit per requirement"
        content_hash = hashlib.sha256(f"preference\nuser\nworkflow\n{value}".encode()).hexdigest()
        memory = SemanticMemory(
            id="memory-api-1",
            namespace="project",
            workspace=self.workspace,
            memory_type="preference",
            subject="user",
            predicate="workflow",
            value=value,
            status="active",
            confidence=0.95,
            importance=0.9,
            source_session_id="session-source",
            source_message_ids=["message-source"],
            content_hash=content_hash,
            extractor_version="test",
            created_at=1,
            updated_at=1,
        )
        asyncio.run(self.api.store.activate_semantic_memory(memory))
        rebuild = self.client.post("/memory/core/rebuild", params={"workspace": self.workspace})
        self.assertEqual(rebuild.status_code, 200, rebuild.text)

        listed = self.client.get("/memory/semantic", params={"workspace": self.workspace})
        self.assertEqual([item["id"] for item in listed.json()["memories"]], [memory.id])
        core = self.client.get("/memory/core", params={"workspace": self.workspace})
        self.assertIn(value, core.json()["rendered"])

        forgotten = self.client.delete(f"/memory/semantic/{memory.id}")
        self.assertEqual(forgotten.status_code, 200, forgotten.text)
        self.assertEqual(forgotten.json()["memory"]["status"], "deleted")
        self.assertEqual(
            self.client.get("/memory/core", params={"workspace": self.workspace}).json()["blocks"],
            [],
        )
        status = self.client.get("/memory/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIn("outbox", status.json())

    def test_subagent_candidate_is_inert_until_explicit_activation(self) -> None:
        value = "configuration is loaded from src/config.py"
        memory = SemanticMemory(
            id="memory-api-candidate",
            namespace="project",
            workspace=self.workspace,
            memory_type="lesson",
            subject="run:child-1",
            predicate="subagent_finding",
            value=value,
            status="candidate",
            confidence=0.6,
            importance=0.5,
            source_session_id="session-child",
            source_run_id="run-child",
            source_kind="subagent",
            source_agent="explore",
            source_message_ids=["message-child"],
            content_hash=hashlib.sha256(value.encode()).hexdigest(),
            extractor_version="subagent-candidate-v1",
            created_at=2,
            updated_at=2,
        )
        asyncio.run(self.api.store.put_semantic_memory(memory))

        active = self.client.get("/memory/semantic", params={"workspace": self.workspace})
        self.assertNotIn(memory.id, [item["id"] for item in active.json()["memories"]])
        candidates = self.client.get(
            "/memory/semantic",
            params={"workspace": self.workspace, "status": "candidate"},
        )
        self.assertEqual([item["id"] for item in candidates.json()["memories"]], [memory.id])

        promoted = self.client.post(f"/memory/semantic/{memory.id}/activate")
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(promoted.json()["action"], "ADD")
        self.assertEqual(promoted.json()["memory"]["status"], "active")
        core = self.client.get("/memory/core", params={"workspace": self.workspace})
        self.assertIn(value, core.json()["rendered"])


if __name__ == "__main__":
    unittest.main()

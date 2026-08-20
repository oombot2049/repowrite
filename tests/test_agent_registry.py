from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from nexapilot.agents.registry import AgentRegistry, load_agent_registry
from nexapilot.agents.service import AgentService
from nexapilot.agents.types import AgentDefinition, AgentLimits
from nexapilot.bus.bus import Bus
from nexapilot.config import load
from nexapilot.hooks import Hooker
from nexapilot.memory.service import MemoryService
from nexapilot.model import PermissionRule, Session
from nexapilot.permission.service import PermissionService
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.tools.base import ToolResult
from nexapilot.tools.task import TaskTool


class FakeMCPManager:
    async def list_tools(self):
        return []


class AgentRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_project_yaml_adds_role_without_replacing_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".nexa"
            config_dir.mkdir()
            (config_dir / "agents.yaml").write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "agents": [
                            {
                                "name": "reviewer",
                                "mode": "subagent",
                                "description": "Review code without editing.",
                                "capabilities": ["code_review", "risk_analysis"],
                                "prompt": "Review evidence and report defects.",
                                "tools": ["read", "glob", "grep"],
                                "permissions": [
                                    {
                                        "permission": "read",
                                        "pattern": "*",
                                        "action": "allow",
                                    },
                                    {
                                        "permission": "*",
                                        "pattern": "*",
                                        "action": "deny",
                                    },
                                ],
                                "limits": {
                                    "max_turns": 6,
                                    "max_tool_calls": 12,
                                    "max_wall_time_ms": 120000,
                                    "max_concurrency": 2,
                                },
                                "model": {
                                    "provider": "openai-compatible",
                                    "id": "review-model",
                                },
                                "workspace": {
                                    "mode": "git_worktree",
                                    "cleanup": "manual",
                                },
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            registry = load_agent_registry(tmp)
            self.assertEqual(
                [agent.name for agent in registry.list()],
                ["primary", "explore", "reviewer"],
            )
            reviewer = registry.get("reviewer")
            self.assertEqual(
                reviewer.load_prompt(), "Review evidence and report defects."
            )
            self.assertEqual(reviewer.capabilities, {"code_review", "risk_analysis"})
            self.assertEqual(reviewer.tool_allowlist, {"read", "glob", "grep"})
            self.assertEqual(reviewer.limits.max_concurrency, 2)
            self.assertEqual(reviewer.model_override.id, "review-model")
            self.assertEqual(reviewer.workspace.mode, "git_worktree")
            self.assertTrue(reviewer.source.endswith("agents.yaml"))

    def test_invalid_registry_is_rejected_at_load_time(self) -> None:
        cases = (
            {
                "version": 1,
                "agents": [
                    {
                        "name": "Bad Name",
                        "mode": "subagent",
                        "description": "bad",
                        "tools": [],
                        "permissions": [],
                    }
                ],
            },
            {
                "version": 1,
                "agents": [
                    {
                        "name": "reviewer",
                        "mode": "subagent",
                        "description": "one",
                        "tools": [],
                        "permissions": [],
                    },
                    {
                        "name": "reviewer",
                        "mode": "subagent",
                        "description": "two",
                        "tools": [],
                        "permissions": [],
                    },
                ],
            },
            {
                "version": 1,
                "agents": [
                    {
                        "name": "reviewer",
                        "mode": "subagent",
                        "description": "bad budget",
                        "tools": [],
                        "permissions": [],
                        "limits": {"max_concurrency": 0},
                    }
                ],
            },
        )
        for document in cases:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as tmp:
                config_dir = Path(tmp) / ".nexa"
                config_dir.mkdir()
                (config_dir / "agents.yaml").write_text(
                    yaml.safe_dump(document), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_agent_registry(tmp)

    def test_prompt_file_cannot_escape_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".nexa"
            config_dir.mkdir()
            (config_dir / "agents.yaml").write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "agents": [
                            {
                                "name": "reviewer",
                                "mode": "subagent",
                                "description": "unsafe prompt",
                                "prompt_file": "../outside.txt",
                                "tools": [],
                                "permissions": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "escapes project root"):
                load_agent_registry(tmp)

    def test_task_tool_schema_is_generated_from_registry(self) -> None:
        registry = AgentRegistry(
            (
                AgentDefinition(name="primary", mode="primary", description="Primary"),
                AgentDefinition(
                    name="reviewer", mode="subagent", description="Reviewer"
                ),
                AgentDefinition(name="tester", mode="subagent", description="Tester"),
            )
        )
        service = SimpleNamespace(registry=registry)
        tool = TaskTool(service=service, parent_session=SimpleNamespace())
        self.assertEqual(
            tool.schema()["properties"]["subagent_type"]["enum"],
            ["reviewer", "tester"],
        )
        self.assertIn("reviewer", tool.description)
        self.assertIn("tester", tool.description)

    async def test_role_concurrency_limit_serializes_parallel_delegations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "registry.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()
            config_dir = Path(tmp) / ".nexa"
            config_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "openai": {
                            "base_url": "http://local.test/v1",
                            "api_key": "test-key",
                            "model": "test-model",
                        },
                        "db_path": db_path,
                        "default_worktree": tmp,
                        "logging": {"console": False, "file": False},
                    }
                ),
                encoding="utf-8",
            )
            cfg = load(tmp)
            bus = Bus()
            hooks = Hooker()
            registry = AgentRegistry(
                (
                    AgentDefinition(
                        name="primary", mode="primary", description="Primary"
                    ),
                    AgentDefinition(
                        name="reviewer",
                        mode="subagent",
                        description="Reviewer",
                        limits=AgentLimits(max_concurrency=1),
                    ),
                )
            )
            service = AgentService(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=PermissionService(bus, store, hooks),
                llm=SimpleNamespace(),
                interrupt=SimpleNamespace(),
                hooks=hooks,
                memory_service=MemoryService(cfg=cfg, store=store),
                daytona_manager=SimpleNamespace(),
                mcp_manager=FakeMCPManager(),
                registry=registry,
            )
            active = 0
            peak = 0

            async def fake_execute(**_kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1
                return ToolResult(title="done", output="done", metadata={})

            service._execute_task = fake_execute
            ctx = SimpleNamespace(ask=lambda **_kwargs: asyncio.sleep(0))
            parent = Session(
                id="parent",
                title="Parent",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[
                    PermissionRule(permission="*", pattern="*", action="allow")
                ],
                root_session_id="parent",
            )

            async def invoke(index: int):
                return await service.execute_task(
                    parent_session=parent,
                    description=f"review-{index}",
                    prompt="review",
                    subagent_type="reviewer",
                    resume_session_id=None,
                    parent_ctx=ctx,
                )

            await asyncio.gather(invoke(1), invoke(2))
            self.assertEqual(peak, 1)

            active = 0
            peak = 0
            approvals: list[dict] = []

            async def approve_batch(**kwargs):
                approvals.append(kwargs)

            service._agent_slots["reviewer"] = asyncio.Semaphore(2)
            batch_ctx = SimpleNamespace(
                ask=approve_batch,
                tool_part_id="batch-call",
                trace_id="trace",
                parent_observation_id=None,
            )
            batch = await service.execute_task_batch(
                parent_session=parent,
                tasks=[
                    {
                        "description": "one",
                        "prompt": "review one",
                        "subagent_type": "reviewer",
                    },
                    {
                        "description": "two",
                        "prompt": "review two",
                        "subagent_type": "reviewer",
                    },
                ],
                parent_ctx=batch_ctx,
            )
            self.assertEqual(peak, 2)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]["permission"], "task")
            self.assertEqual(batch.metadata["task_count"], 2)
            self.assertEqual(batch.metadata["completed_count"], 2)

            attempts = 0

            async def fail_once(**_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("delegation failed")
                return ToolResult(title="recovered", output="recovered", metadata={})

            service._execute_task = fail_once
            with self.assertRaisesRegex(RuntimeError, "delegation failed"):
                await invoke(3)
            recovered = await asyncio.wait_for(invoke(4), timeout=0.5)
            self.assertEqual(recovered.output, "recovered")

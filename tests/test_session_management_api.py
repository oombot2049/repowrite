from __future__ import annotations

import asyncio
import importlib
import json
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))


class SessionManagementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._db_tmp = tempfile.TemporaryDirectory()
        cls._worktree_tmp = tempfile.TemporaryDirectory()
        cls._config_tmp = tempfile.TemporaryDirectory()

        # Create a temporary config file for the app
        import yaml

        config_dir = Path(cls._config_tmp.name) / ".nexa"
        config_dir.mkdir(parents=True)
        config_data = {
            "openai": {
                "base_url": "http://local.test/v1",
                "api_key": "test-key",
                "model": "test-model",
            },
            "langfuse": {"enabled": False},
            "logging": {"console": False, "file": False},
            "db_path": str(Path(cls._db_tmp.name) / "test.sqlite3"),
            "default_worktree": cls._worktree_tmp.name,
        }
        (config_dir / "config.yaml").write_text(yaml.dump(config_data))

        # Patch config.load to use our temp config
        import nexapilot.config as _cfg_mod

        cls._orig_global_paths = _cfg_mod.GLOBAL_CONFIG_PATHS
        _cfg_mod.GLOBAL_CONFIG_PATHS = (str(config_dir / "config.yaml"),)

        if "nexapilot.api.app" in sys.modules:
            del sys.modules["nexapilot.api.app"]
        api_app = importlib.import_module("nexapilot.api.app")
        cls.api_app = api_app
        cls.client = TestClient(api_app.app)
        cls.client.__enter__()
        cls.db_path = str(Path(cls._db_tmp.name) / "test.sqlite3")
        cls.worktree = cls._worktree_tmp.name

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls._db_tmp.cleanup()
        cls._worktree_tmp.cleanup()
        cls._config_tmp.cleanup()
        # Restore global config paths
        import nexapilot.config as _cfg_mod

        _cfg_mod.GLOBAL_CONFIG_PATHS = cls._orig_global_paths

    def setUp(self) -> None:
        self._clear_all_tables()

    def _clear_all_tables(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("DELETE FROM agent_workspaces")
            db.execute("DELETE FROM artifacts")
            db.execute("DELETE FROM llm_call_attempts")
            db.execute("DELETE FROM llm_calls")
            db.execute("DELETE FROM provider_circuits")
            db.execute("DELETE FROM run_steps")
            db.execute("DELETE FROM session_leases")
            db.execute("DELETE FROM tool_operations")
            db.execute("DELETE FROM cron_job_runs")
            db.execute("DELETE FROM cron_jobs")
            db.execute("DELETE FROM runs")
            db.execute("DELETE FROM parts")
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM permission_requests")
            db.execute("DELETE FROM permission_approvals")
            db.execute("DELETE FROM todos")
            db.execute("DELETE FROM plan_task_dependencies")
            db.execute("DELETE FROM plan_tasks")
            db.execute("DELETE FROM task_plans")
            db.execute("DELETE FROM task_runtime_events")
            db.execute("DELETE FROM goals")
            db.execute("DELETE FROM outbox_events")
            db.execute("DELETE FROM sessions")
            db.commit()

    def test_run_workspace_and_artifact_download_contract(self) -> None:
        session_response = self.client.post(
            "/sessions",
            json={"title": "Artifacts", "worktree": self.worktree},
        )
        self.assertEqual(session_response.status_code, 200)
        session = session_response.json()
        run = asyncio.run(
            self.api_app.store.create_run(
                session_id=session["id"],
                trigger_message_id=None,
                source="api",
                agent_name="primary",
                model=self.api_app.ModelRef(
                    provider="openai-compatible", id="test-model"
                ),
            )
        )
        artifact = asyncio.run(
            self.api_app.artifact_store.put_bytes(
                session_id=session["id"],
                run_id=run.id,
                message_id=None,
                tool_call_id=None,
                kind="report",
                name="report.txt",
                media_type="text/plain",
                content=b"verified evidence",
            )
        )

        workspace_response = self.client.get(f"/runs/{run.id}/workspace")
        self.assertEqual(workspace_response.status_code, 200)
        workspace = workspace_response.json()
        self.assertEqual(workspace["run"]["id"], run.id)
        self.assertEqual(workspace["artifacts"][0]["id"], artifact.id)
        self.assertNotIn("storage_path", workspace["artifacts"][0])

        metadata_response = self.client.get(f"/artifacts/{artifact.id}")
        self.assertEqual(metadata_response.status_code, 200)
        self.assertNotIn("storage_path", metadata_response.json())
        download = self.client.get(f"/artifacts/{artifact.id}/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"verified evidence")
        self.assertEqual(
            download.headers["etag"], f'"sha256:{artifact.sha256}"'
        )
        self.assertIn("attachment", download.headers["content-disposition"])

    def _insert_running_run(self, session_id: str) -> str:
        run_id = str(uuid4())
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """
                INSERT INTO runs (
                  id,session_id,sequence,status,source,agent_name,
                  model_provider,model_id,started_at,created_at,updated_at,
                  owner_id,lease_until,heartbeat_at,attempt
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    session_id,
                    1,
                    "running",
                    "test",
                    "primary",
                    "openai-compatible",
                    "test-model",
                    now,
                    now,
                    now,
                    "api-test-worker",
                    now + 20_000,
                    now,
                    1,
                ),
            )
            db.commit()
        return run_id

    def _create_session(self, title: str = "Original") -> dict:
        res = self.client.post(
            "/sessions",
            json={
                "worktree": self.worktree,
                "title": title,
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_create_session_defaults_to_local_runtime(self) -> None:
        session = self._create_session("Runtime Default")
        self.assertEqual(session.get("runtime", {}).get("backend"), "local")

    def test_run_cancel_and_step_endpoints_are_durable_and_idempotent(self) -> None:
        session = self._create_session("Durable Run API")
        run_id = self._insert_running_run(session["id"])
        step_id = str(uuid4())
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """
                INSERT INTO run_steps (
                  id,run_id,sequence,kind,status,started_at,metadata_json
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (step_id, run_id, 1, "model", "running", now, "{}"),
            )
            db.commit()

        first = self.client.post(f"/runs/{run_id}/cancel")
        self.assertEqual(first.status_code, 200, first.text)
        first_run = first.json()["run"]
        self.assertEqual(first_run["status"], "cancelling")
        self.assertIsNotNone(first_run["cancel_requested_at"])
        first_revision = first_run["revision"]

        repeated = self.client.post(f"/runs/{run_id}/cancel")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["run"]["revision"], first_revision)

        steps = self.client.get(f"/runs/{run_id}/steps")
        self.assertEqual(steps.status_code, 200, steps.text)
        self.assertEqual(steps.json()["steps"][0]["id"], step_id)

        operations = self.client.get(f"/runs/{run_id}/operations")
        self.assertEqual(operations.status_code, 200, operations.text)
        self.assertEqual(operations.json()["operations"], [])

    def test_provider_capability_and_audit_endpoints_are_redacted(self) -> None:
        capabilities = self.client.get("/providers/capabilities")
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        capability_payload = capabilities.json()
        self.assertEqual(capability_payload["configured_transport"], "auto")
        self.assertEqual(capability_payload["capabilities"]["model"], "test-model")
        self.assertNotIn("test-key", capabilities.text)

        status = self.client.get("/providers/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["circuits"], [])
        self.assertNotIn("test-key", status.text)

        session = self._create_session("Provider audit")
        run_id = self._insert_running_run(session["id"])
        step_id = str(uuid4())
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """
                INSERT INTO run_steps (
                  id,run_id,sequence,kind,status,started_at,metadata_json
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (step_id, run_id, 1, "model", "running", now, "{}"),
            )
            db.execute(
                """
                INSERT INTO llm_calls (
                  id,run_id,step_id,provider,endpoint_hash,model,transport,
                  capability_profile_version,request_hash,status,started_at,
                  metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "call-1",
                    run_id,
                    step_id,
                    "openai-compatible",
                    "endpoint-hash",
                    "test-model",
                    "responses",
                    "test-v1",
                    "request-hash",
                    "completed",
                    now,
                    "{}",
                ),
            )
            db.execute(
                """
                INSERT INTO llm_call_attempts (
                  id,call_id,attempt,status,started_at,finished_at
                ) VALUES (?,?,?,?,?,?)
                """,
                ("attempt-1", "call-1", 1, "completed", now, now + 1),
            )
            db.commit()

        calls = self.client.get(f"/runs/{run_id}/llm-calls")
        self.assertEqual(calls.status_code, 200, calls.text)
        self.assertEqual(calls.json()["calls"][0]["id"], "call-1")
        attempts = self.client.get("/llm-calls/call-1/attempts")
        self.assertEqual(attempts.status_code, 200, attempts.text)
        self.assertEqual(attempts.json()["attempts"][0]["id"], "attempt-1")

    def test_agent_registry_endpoint_exposes_effective_roles(self) -> None:
        response = self.client.get("/agents")
        self.assertEqual(response.status_code, 200, response.text)
        agents = {agent["name"]: agent for agent in response.json()["agents"]}
        self.assertEqual(set(agents), {"primary", "explore"})
        self.assertEqual(agents["explore"]["mode"], "subagent")
        self.assertIn("code_search", agents["explore"]["capabilities"])
        self.assertEqual(agents["explore"]["limits"]["max_concurrency"], 2)
        self.assertEqual(agents["explore"]["limits"]["max_input_tokens"], 16_000)
        self.assertEqual(agents["explore"]["workspace"]["mode"], "shared")
        self.assertNotIn("api_key", response.text.lower())

    def test_agent_workspace_api_lists_and_handles_missing_resource(self) -> None:
        response = self.client.get("/agent-workspaces")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"workspaces": []})
        self.assertEqual(
            self.client.get("/agent-workspaces/not-found").status_code, 404
        )
        self.assertEqual(
            self.client.post(
                "/agent-workspaces/not-found/release", json={"force": False}
            ).status_code,
            404,
        )

    def test_console_uses_local_assets_nonce_csp_and_security_headers(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("https://unpkg.com", response.text)
        self.assertIn("/static/vendor/preact.umd.js", response.text)
        self.assertIn("/static/vendor/purify.min.js", response.text)
        csp = response.headers.get("content-security-policy", "")
        match = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
        self.assertIsNotNone(match, csp)
        assert match is not None
        self.assertIn(f'nonce="{match.group(1)}"', response.text)
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        self.assertEqual(self.client.get("/static/index.html").status_code, 404)
        self.assertEqual(self.client.get("/static/index-old.html").status_code, 404)

    def test_config_api_never_returns_secret_values(self) -> None:
        response = self.client.get("/config")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["openai"]["api_key"], {"configured": True})
        self.assertNotIn("test-key", response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_raw_config_is_redacted_and_round_trip_preserves_secret(self) -> None:
        response = self.client.get("/config/raw", params={"scope": "global"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertNotIn("test-key", response.text)
        self.assertIn("<redacted>", payload["content"])
        self.assertIn("openai.api_key", payload["sensitive_fields_set"])

        saved = self.client.put(
            "/config/raw",
            json={"scope": "global", "content": payload["content"]},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        configured = Path(payload["path"]).read_text(encoding="utf-8")
        self.assertIn("test-key", configured)
        self.assertNotIn("<redacted>", configured)

    def test_cross_origin_mutation_is_rejected_without_side_effect(self) -> None:
        response = self.client.post(
            "/sessions",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
            json={"worktree": self.worktree, "title": "Must not exist"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "cross_site_request_denied")
        listed = self.client.get("/sessions").json()["sessions"]
        self.assertFalse(
            any(session["title"] == "Must not exist" for session in listed)
        )

    def test_create_daytona_session_returns_sandbox_id(self) -> None:
        from nexapilot.model import DaytonaRuntimeConfig, SessionRuntime

        async def _ensure(session):
            return session.model_copy(
                update={
                    "runtime": SessionRuntime(
                        backend="daytona",
                        daytona=DaytonaRuntimeConfig(
                            sandbox_id="sbx-test", sandbox_name="demo-sandbox"
                        ),
                    )
                },
            )

        with patch(
            "nexapilot.api.app.daytona_manager.ensure_session_runtime_async",
            new=AsyncMock(side_effect=_ensure),
        ) as mocked:
            res = self.client.post(
                "/sessions",
                json={
                    "worktree": "/workspace",
                    "title": "Daytona Session",
                    "runtime": {"backend": "daytona"},
                },
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["runtime"]["backend"], "daytona")
            self.assertEqual(data["runtime"]["daytona"]["sandbox_id"], "sbx-test")
            self.assertEqual(data["runtime"]["daytona"]["sandbox_name"], "demo-sandbox")
            self.assertEqual(mocked.await_count, 1)

    def test_create_daytona_session_uses_remote_default_workspace_when_local_default_passed(
        self,
    ) -> None:
        from nexapilot.model import DaytonaRuntimeConfig, SessionRuntime

        async def _ensure(session):
            # Ensure API normalized worktree before runtime init.
            self.assertEqual(session.worktree, "/workspace")
            self.assertEqual(session.cwd, "/workspace")
            return session.model_copy(
                update={
                    "runtime": SessionRuntime(
                        backend="daytona",
                        daytona=DaytonaRuntimeConfig(
                            sandbox_id="sbx-remote", sandbox_name="remote-name"
                        ),
                    ),
                },
            )

        with patch(
            "nexapilot.api.app.daytona_manager.ensure_session_runtime_async",
            new=AsyncMock(side_effect=_ensure),
        ):
            res = self.client.post(
                "/sessions",
                json={
                    "worktree": self.worktree,
                    "title": "Daytona Session",
                    "runtime": {"backend": "daytona"},
                },
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["worktree"], "/workspace")
            self.assertEqual(data["cwd"], "/workspace")

    def test_create_daytona_session_rolls_back_when_runtime_init_fails(self) -> None:
        from nexapilot.runtime import DaytonaOperationError

        with patch(
            "nexapilot.api.app.daytona_manager.ensure_session_runtime_async",
            new=AsyncMock(side_effect=DaytonaOperationError("daytona failed")),
        ):
            res = self.client.post(
                "/sessions",
                json={
                    "worktree": "/workspace",
                    "title": "Bad Daytona",
                    "runtime": {"backend": "daytona"},
                },
            )
            self.assertEqual(res.status_code, 502, res.text)

        listed = self.client.get("/sessions")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json().get("sessions", [])), 0)

    def test_build_tools_switches_by_runtime_backend(self) -> None:
        app_module = importlib.import_module("nexapilot.api.app")
        from nexapilot.model import DaytonaRuntimeConfig, SessionRuntime
        from nexapilot.tools.bash import BashTool
        from nexapilot.tools.daytona import DaytonaBashTool

        session = self._create_session("Tool Runtime")
        session_obj = asyncio.run(app_module.store.get_session(session["id"]))
        tools_local = asyncio.run(app_module._build_tools(session_obj))
        self.assertIsInstance(tools_local.get("bash"), BashTool)
        self.assertEqual(tools_local.get("memory_search").name, "memory_search")
        self.assertEqual(tools_local.get("memory_get").name, "memory_get")

        session_daytona = session_obj.model_copy(
            update={
                "runtime": SessionRuntime(
                    backend="daytona", daytona=DaytonaRuntimeConfig(sandbox_id="sbx-1")
                )
            },
        )
        fake_sandbox_ref = SimpleNamespace(
            sandbox_id="sbx-1",
            sandbox=SimpleNamespace(process=SimpleNamespace(), fs=SimpleNamespace()),
        )
        with patch(
            "nexapilot.api.app.daytona_manager.get_sandbox_for_session",
            new=AsyncMock(return_value=fake_sandbox_ref),
        ):
            tools_daytona = asyncio.run(app_module._build_tools(session_daytona))
            self.assertIsInstance(tools_daytona.get("bash"), DaytonaBashTool)
            with self.assertRaises(KeyError):
                tools_daytona.get("memory_search")

    def test_rename_session(self) -> None:
        session = self._create_session("Before Rename")
        session_id = session["id"]
        before_updated_at = int(session["updated_at"])

        time.sleep(0.01)  # Ensure updated_at has a chance to move forward.
        res = self.client.patch(
            f"/sessions/{session_id}", json={"title": "After Rename"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        updated = res.json()

        self.assertEqual(updated["id"], session_id)
        self.assertEqual(updated["title"], "After Rename")
        self.assertGreaterEqual(int(updated["updated_at"]), before_updated_at)

        listed = self.client.get("/sessions")
        self.assertEqual(listed.status_code, 200, listed.text)
        listed_sessions = listed.json()["sessions"]
        self.assertEqual(len(listed_sessions), 1)
        self.assertEqual(listed_sessions[0]["title"], "After Rename")

    def test_create_session_archives_previous_session_into_daily_memory_log(
        self,
    ) -> None:
        worktree = Path(self.worktree) / "archive-worktree"
        worktree.mkdir(exist_ok=True)

        first = self.client.post(
            "/sessions",
            json={"worktree": str(worktree), "title": "First Session"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_session = first.json()

        add_msg_res = self.client.post(
            f"/sessions/{first_session['id']}/messages",
            json={"text": "Need to remember the beta rollout plan"},
        )
        self.assertEqual(add_msg_res.status_code, 200, add_msg_res.text)

        second = self.client.post(
            "/sessions",
            json={"worktree": str(worktree), "title": "Second Session"},
        )
        self.assertEqual(second.status_code, 200, second.text)

        archive_path = (
            worktree
            / "memory"
            / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.md"
        )
        self.assertTrue(archive_path.is_file())
        content = archive_path.read_text(encoding="utf-8")
        self.assertIn("First Session", content)
        self.assertIn("Need to remember the beta rollout plan", content)

    def test_rename_session_requires_non_empty_title(self) -> None:
        session = self._create_session("Keep Name")
        session_id = session["id"]

        res = self.client.patch(f"/sessions/{session_id}", json={"title": "   "})
        self.assertEqual(res.status_code, 400)

    def test_durable_goal_plan_task_api_controls_and_audit(self) -> None:
        session = self._create_session("Durable plan API")
        create = self.client.post(
            f"/sessions/{session['id']}/goals",
            json={
                "title": "Implement and verify feature",
                "rationale": "dependency ordered delivery",
                "tasks": [
                    {"key": "implement", "title": "Implement", "priority": 10},
                    {
                        "key": "verify",
                        "title": "Verify",
                        "depends_on": ["implement"],
                    },
                ],
            },
        )
        self.assertEqual(create.status_code, 200, create.text)
        graph = create.json()
        goal = graph["goal"]
        plan = graph["plans"][0]
        implement = next(task for task in graph["tasks"] if task["key"] == "implement")

        ready = self.client.get(f"/plans/{plan['id']}/ready-tasks")
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual([task["key"] for task in ready.json()["tasks"]], ["implement"])

        paused = self.client.post(
            f"/goals/{goal['id']}/pause",
            json={"expected_revision": goal["revision"], "reason": "review"},
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["goal"]["status"], "paused")
        self.assertEqual(
            self.client.get(f"/plans/{plan['id']}/ready-tasks").json()["tasks"], []
        )
        stale = self.client.post(
            f"/goals/{goal['id']}/resume",
            json={"expected_revision": goal["revision"]},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        resumed = self.client.post(
            f"/goals/{goal['id']}/resume",
            json={"expected_revision": paused.json()["goal"]["revision"]},
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)

        takeover = self.client.post(
            f"/tasks/{implement['id']}/takeover",
            json={
                "expected_revision": implement["revision"],
                "assignee": "reviewer",
                "reason": "manual patch",
            },
        )
        self.assertEqual(takeover.status_code, 200, takeover.text)
        human_task = next(
            task for task in takeover.json()["tasks"] if task["id"] == implement["id"]
        )
        self.assertEqual(human_task["execution_mode"], "human")
        completed = self.client.post(
            f"/tasks/{implement['id']}/transition",
            json={
                "status": "completed",
                "expected_revision": human_task["revision"],
                "actor": "reviewer",
                "result": {"commit": "abc123"},
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        ready_after = self.client.get(f"/plans/{plan['id']}/ready-tasks").json()["tasks"]
        self.assertEqual([task["key"] for task in ready_after], ["verify"])

        events = self.client.get(f"/goals/{goal['id']}/events")
        self.assertEqual(events.status_code, 200, events.text)
        event_types = [event["event_type"] for event in events.json()["events"]]
        self.assertIn("goal.paused", event_types)
        self.assertIn("goal.resumed", event_types)
        self.assertIn("task.taken_over", event_types)
        self.assertIn("task.completed", event_types)

    def test_delete_session_cascades_related_rows(self) -> None:
        session = self._create_session("To Be Deleted")
        session_id = session["id"]

        add_msg_res = self.client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "hello"},
        )
        self.assertEqual(add_msg_res.status_code, 200, add_msg_res.text)

        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """
                INSERT INTO todos (id, session_id, content, status, priority, active_form, position, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "todo-1",
                    session_id,
                    "task",
                    "pending",
                    "medium",
                    "Working...",
                    0,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO permission_requests (
                    id, session_id, permission, patterns_json, metadata_json, always_json, tool_json, status, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "req-1",
                    session_id,
                    "read",
                    json.dumps(["*"]),
                    json.dumps({}),
                    json.dumps([]),
                    None,
                    "pending",
                    now,
                    None,
                ),
            )
            db.execute(
                """
                INSERT INTO permission_approvals (session_id, permission, pattern, action)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, "read", "*", "allow"),
            )
            db.execute(
                """
                INSERT INTO cron_jobs (
                    id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                    payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error, last_assistant_message_id,
                    last_trace_id, delete_after_run, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-1",
                    "cleanup-test",
                    session_id,
                    1,
                    "every",
                    None,
                    60000,
                    None,
                    None,
                    "agent_turn",
                    "ping",
                    now + 60000,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO cron_job_runs (job_id, session_id, started_at, finished_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("job-1", session_id, now, now + 10, "ok"),
            )
            db.commit()

        delete_res = self.client.delete(f"/sessions/{session_id}")
        self.assertEqual(delete_res.status_code, 200, delete_res.text)
        payload = delete_res.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session_id"], session_id)

        listed = self.client.get("/sessions")
        self.assertEqual(listed.status_code, 200, listed.text)
        session_ids = [s["id"] for s in listed.json()["sessions"]]
        self.assertNotIn(session_id, session_ids)

        with closing(sqlite3.connect(self.db_path)) as db:

            def count_rows(table: str) -> int:
                cur = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)
                )
                return int(cur.fetchone()[0])

            self.assertEqual(count_rows("messages"), 0)
            self.assertEqual(count_rows("parts"), 0)
            self.assertEqual(count_rows("todos"), 0)
            self.assertEqual(count_rows("permission_requests"), 0)
            self.assertEqual(count_rows("permission_approvals"), 0)
            self.assertEqual(count_rows("cron_jobs"), 0)
            self.assertEqual(count_rows("cron_job_runs"), 0)
            self.assertEqual(count_rows("runs"), 0)
            cur = db.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (session_id,))
            self.assertEqual(int(cur.fetchone()[0]), 0)

    def test_delete_missing_session_returns_404(self) -> None:
        res = self.client.delete("/sessions/not-found")
        self.assertEqual(res.status_code, 404)

    def test_delete_session_requires_retained_workspace_release(self) -> None:
        session = self._create_session("Workspace Owner")
        now = int(time.time() * 1_000)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """
                INSERT INTO agent_workspaces (
                  id,child_session_id,root_session_id,repository_root,
                  worktree_path,branch_name,base_commit,status,cleanup_policy,
                  created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "workspace-retained",
                    "child-session",
                    session["id"],
                    self.worktree,
                    str(Path(self.worktree) / "child"),
                    "nexapilot/child",
                    "base",
                    "retained",
                    "manual",
                    now,
                    now,
                ),
            )
            db.commit()

        blocked = self.client.delete(f"/sessions/{session['id']}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE agent_workspaces SET status='released' WHERE id=?",
                ("workspace-retained",),
            )
            db.commit()
        deleted = self.client.delete(f"/sessions/{session['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_channels_status_endpoint(self) -> None:
        res = self.client.get("/channels/status")
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertIn("enabled_channels", data)
        self.assertIn("channels", data)
        self.assertIn("bridge_running", data)
        self.assertIn("queue", data)

    def test_channels_config_and_manage_endpoints(self) -> None:
        get_res = self.client.get("/channels/config")
        self.assertEqual(get_res.status_code, 200, get_res.text)
        cfg = get_res.json()
        self.assertIn("channels", cfg)
        self.assertIn("feishu", cfg["channels"])

        put_res = self.client.put(
            "/channels/config/feishu",
            json={
                "enabled": False,
                "app_id": "cli_test",
                "app_secret": "",
                "encrypt_key": "",
                "verification_token": "",
                "allow_from": ["ou_1", "ou_2"],
                "permission_mode": "commands",
                "allowed_bash_commands": ["git status", "ls *"],
            },
        )
        self.assertEqual(put_res.status_code, 200, put_res.text)
        put_data = put_res.json()
        self.assertTrue(put_data["ok"])
        self.assertIn("channels", put_data)
        self.assertIn("feishu", put_data["channels"])
        self.assertEqual(put_data["channels"]["feishu"]["app_id"], "cli_test")
        self.assertEqual(put_data["channels"]["feishu"]["allow_from"], ["ou_1", "ou_2"])
        self.assertEqual(put_data["channels"]["feishu"]["permission_mode"], "commands")
        self.assertEqual(
            put_data["channels"]["feishu"]["allowed_bash_commands"],
            ["git status", "ls *"],
        )

        partial_res = self.client.put(
            "/channels/config/feishu",
            json={"permission_mode": "allow"},
        )
        self.assertEqual(partial_res.status_code, 200, partial_res.text)
        partial_data = partial_res.json()
        self.assertEqual(partial_data["channels"]["feishu"]["permission_mode"], "allow")
        self.assertEqual(partial_data["channels"]["feishu"]["app_id"], "cli_test")

        bad_mode_res = self.client.put(
            "/channels/config/feishu",
            json={"permission_mode": "invalid"},
        )
        self.assertEqual(bad_mode_res.status_code, 400, bad_mode_res.text)

        connect_res = self.client.post("/channels/feishu/connect")
        self.assertEqual(connect_res.status_code, 404, connect_res.text)

        disconnect_res = self.client.post("/channels/feishu/disconnect")
        self.assertEqual(disconnect_res.status_code, 404, disconnect_res.text)

        test_res = self.client.post("/channels/feishu/test")
        self.assertEqual(test_res.status_code, 404, test_res.text)

    def test_cronjobs_crud_endpoints(self) -> None:
        session = self._create_session("Cron Session")

        create_res = self.client.post(
            "/cronjobs",
            json={
                "name": "Heartbeat",
                "session_id": session["id"],
                "message": "please summarize latest progress",
                "schedule": {"kind": "every", "every_ms": 3600000},
            },
        )
        self.assertEqual(create_res.status_code, 200, create_res.text)
        job = create_res.json()
        job_id = job["id"]
        self.assertEqual(job["name"], "Heartbeat")
        self.assertEqual(job["session_id"], session["id"])
        self.assertTrue(job["enabled"])
        self.assertIsNotNone(job["state"]["next_run_at_ms"])

        list_res = self.client.get("/cronjobs")
        self.assertEqual(list_res.status_code, 200, list_res.text)
        jobs = list_res.json()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], job_id)

        get_res = self.client.get(f"/cronjobs/{job_id}")
        self.assertEqual(get_res.status_code, 200, get_res.text)
        self.assertEqual(get_res.json()["id"], job_id)

        disable_res = self.client.post(
            f"/cronjobs/{job_id}/enabled", json={"enabled": False}
        )
        self.assertEqual(disable_res.status_code, 200, disable_res.text)
        disabled = disable_res.json()
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["state"]["next_run_at_ms"])

        runs_res = self.client.get(f"/cronjobs/{job_id}/runs")
        self.assertEqual(runs_res.status_code, 200, runs_res.text)
        self.assertEqual(runs_res.json()["runs"], [])

        delete_res = self.client.delete(f"/cronjobs/{job_id}")
        self.assertEqual(delete_res.status_code, 200, delete_res.text)
        self.assertTrue(delete_res.json()["ok"])

        missing_res = self.client.get(f"/cronjobs/{job_id}")
        self.assertEqual(missing_res.status_code, 404, missing_res.text)

    def test_cronjobs_status_and_validation(self) -> None:
        status_res = self.client.get("/cronjobs/status")
        self.assertEqual(status_res.status_code, 200, status_res.text)
        status = status_res.json()
        self.assertIn("running", status)
        self.assertIn("jobs", status)
        self.assertIn("next_wake_at_ms", status)

        session = self._create_session("Cron Validation")
        bad_res = self.client.post(
            "/cronjobs",
            json={
                "name": "Bad Cron",
                "session_id": session["id"],
                "message": "hello",
                "schedule": {"kind": "every"},
            },
        )
        self.assertEqual(bad_res.status_code, 400, bad_res.text)

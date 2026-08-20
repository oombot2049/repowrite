from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient


class ProjectThreadApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.project_root = root / "sample-project"
        cls.project_root.mkdir()
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
                    "default_worktree": str(cls.project_root),
                    "memory": {"enabled": False},
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

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        import nexapilot.config as config_module

        config_module.GLOBAL_CONFIG_PATHS = cls._original_paths
        cls._tmp.cleanup()

    def test_project_owns_threads_and_enforces_root_boundary(self) -> None:
        created = self.client.post(
            "/projects",
            json={"name": "Sample", "root_path": str(self.project_root)},
        )
        self.assertEqual(created.status_code, 200, created.text)
        project = created.json()
        self.assertEqual(project["root_path"], str(self.project_root.resolve()))

        duplicate = self.client.post(
            "/projects", json={"name": "Duplicate", "root_path": str(self.project_root)}
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        thread = self.client.post(
            "/sessions",
            json={"project_id": project["id"], "title": "First thread"},
        )
        self.assertEqual(thread.status_code, 200, thread.text)
        thread_data = thread.json()
        self.assertEqual(thread_data["project_id"], project["id"])
        self.assertEqual(thread_data["worktree"], project["root_path"])
        self.assertEqual(thread_data["cwd"], project["root_path"])

        mismatch = self.client.post(
            "/sessions",
            json={
                "project_id": project["id"],
                "worktree": str(Path(self._tmp.name).resolve()),
            },
        )
        self.assertEqual(mismatch.status_code, 409, mismatch.text)

        outside_cwd = self.client.post(
            "/sessions",
            json={
                "project_id": project["id"],
                "cwd": str(Path(self._tmp.name).resolve()),
            },
        )
        self.assertEqual(outside_cwd.status_code, 400, outside_cwd.text)

        projects = self.client.get("/projects")
        self.assertEqual(projects.status_code, 200, projects.text)
        self.assertEqual(projects.json()["projects"][0]["thread_count"], 1)

        threads = self.client.get(f"/projects/{project['id']}/threads")
        self.assertEqual(threads.status_code, 200, threads.text)
        self.assertEqual(
            [item["id"] for item in threads.json()["threads"]],
            [thread_data["id"]],
        )

        renamed = self.client.patch(
            f"/projects/{project['id']}", json={"name": "Renamed project"}
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "Renamed project")

        blocked = self.client.delete(f"/projects/{project['id']}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(
            self.client.delete(f"/sessions/{thread_data['id']}").status_code, 200
        )
        deleted = self.client.delete(f"/projects/{project['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_project_rejects_missing_and_relative_roots(self) -> None:
        relative = self.client.post(
            "/projects", json={"name": "Relative", "root_path": "relative/path"}
        )
        self.assertEqual(relative.status_code, 400, relative.text)
        missing = self.client.post(
            "/projects",
            json={
                "name": "Missing",
                "root_path": str(Path(self._tmp.name) / "not-created"),
            },
        )
        self.assertEqual(missing.status_code, 400, missing.text)


if __name__ == "__main__":
    unittest.main()

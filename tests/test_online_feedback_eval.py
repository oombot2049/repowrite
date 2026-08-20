from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient

from nexapilot.evaluation.feedback import redact_feedback_text
from nexapilot.model import Message, ModelRef, TextPart


class FeedbackRedactionTests(unittest.TestCase):
    def test_common_secrets_and_personal_paths_are_removed(self) -> None:
        text = (
            "api_key=top-secret-value email me@example.com "
            "token sk-proj-abcdefghijklmnopqrstuvwxyz "
            r"file C:\Users\alice\repo\config.yaml"
        )

        redacted, count = redact_feedback_text(text)

        self.assertGreaterEqual(count, 4)
        self.assertNotIn("top-secret-value", redacted)
        self.assertNotIn("me@example.com", redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertNotIn("alice", redacted)


class OnlineFeedbackEvalApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.worktree = root / "worktree"
        cls.worktree.mkdir()
        cls.db_path = root / "test.sqlite3"
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
                    "db_path": str(cls.db_path),
                    "default_worktree": str(cls.worktree),
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

    def _seed_terminal_run(self, *, status: str = "completed") -> tuple[str, str]:
        session_response = self.client.post(
            "/sessions",
            json={
                "title": "Feedback test",
                "worktree": str(self.worktree),
                "cwd": str(self.worktree),
            },
        )
        self.assertEqual(session_response.status_code, 200, session_response.text)
        session_id = session_response.json()["id"]
        user_response = self.client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "Fix it with api_key=source-secret"},
        )
        self.assertEqual(user_response.status_code, 200, user_response.text)
        user_message_id = user_response.json()["message_id"]

        async def seed() -> str:
            run = await self.api.store.create_run(
                session_id=session_id,
                trigger_message_id=user_message_id,
                source="test",
                agent_name="primary",
                model=ModelRef(provider="test", id="test-model"),
            )
            await self.api.store.attach_message_to_run(user_message_id, run.id)
            now = int(time.time() * 1000)
            assistant_id = str(uuid4())
            assistant = await self.api.store.add_message(
                Message(
                    id=assistant_id,
                    session_id=session_id,
                    run_id=run.id,
                    role="assistant",
                    agent="primary",
                    model=ModelRef(provider="test", id="test-model"),
                    created_at=now,
                )
            )
            await self.api.store.add_part(
                session_id,
                assistant.id,
                TextPart(
                    id=str(uuid4()),
                    message_id=assistant.id,
                    session_id=session_id,
                    text="I used sk-proj-response-secret-value in the answer",
                ),
            )
            await self.api.store.finish_run(
                run.id,
                status=status,
                assistant_message_id=assistant.id,
                finish_reason=status,
                error={"message": "tool failed"} if status == "failed" else None,
            )
            return run.id

        return session_id, asyncio.run(seed())

    def test_negative_feedback_creates_redacted_review_gated_bad_case(self) -> None:
        session_id, run_id = self._seed_terminal_run(status="failed")
        secret = "private-comment-secret"
        response = self.client.post(
            f"/runs/{run_id}/feedback",
            json={
                "rating": "negative",
                "error_types": ["tool_failure", "incorrect"],
                "comment": f"password={secret}; contact owner@example.com",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["created"])
        self.assertEqual(payload["feedback"]["rating"], "negative")
        candidate = payload["candidate"]
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["run_status"], "failed")
        self.assertNotIn("source-secret", candidate["prompt_redacted"])
        self.assertNotIn("response-secret", candidate["response_redacted"])
        self.assertNotIn(secret, candidate["feedback_redacted"])
        self.assertNotIn("owner@example.com", candidate["feedback_redacted"])

        listed = self.client.get(
            "/evaluation/candidates",
            params={"session_id": session_id, "status": "pending"},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([candidate["id"]], [item["id"] for item in listed.json()["candidates"]])

        reviewed = self.client.post(
            f"/evaluation/candidates/{candidate['id']}/review",
            json={"decision": "accept", "note": "Reproduce before adding checks"},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["candidate"]["status"], "accepted")
        self.assertFalse(reviewed.json()["baseline_promoted"])

        db = sqlite3.connect(self.db_path)
        try:
            stored_comment = db.execute(
                "SELECT comment_redacted FROM run_feedback WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        finally:
            db.close()
        self.assertNotIn(secret, stored_comment)

    def test_feedback_is_idempotent_but_immutable(self) -> None:
        _session_id, run_id = self._seed_terminal_run()
        body = {"rating": "positive", "comment": "Good result"}
        first = self.client.post(f"/runs/{run_id}/feedback", json=body)
        second = self.client.post(f"/runs/{run_id}/feedback", json=body)
        conflict = self.client.post(
            f"/runs/{run_id}/feedback",
            json={"rating": "negative", "error_types": ["other"]},
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["created"])
        self.assertIsNone(first.json()["candidate"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["created"])
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_active_run_and_invalid_feedback_are_rejected(self) -> None:
        session = self.client.post(
            "/sessions",
            json={"worktree": str(self.worktree), "cwd": str(self.worktree)},
        ).json()
        run = asyncio.run(
            self.api.store.create_run(
                session_id=session["id"],
                trigger_message_id=None,
                source="test",
                agent_name="primary",
                model=ModelRef(provider="test", id="test-model"),
            )
        )

        active = self.client.post(
            f"/runs/{run.id}/feedback", json={"rating": "positive"}
        )
        invalid = self.client.post(
            f"/runs/{run.id}/feedback", json={"rating": "negative"}
        )
        self.assertEqual(active.status_code, 409, active.text)
        self.assertEqual(invalid.status_code, 422, invalid.text)


if __name__ == "__main__":
    unittest.main()

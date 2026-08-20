from __future__ import annotations

import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nexapilot.bus.bus import Bus
from nexapilot.config import _build_config
from nexapilot.llm.gateway import ProviderGateway
from nexapilot.llm.openai_chat import Finish, LLMEvent, ResponseStarted, TextDelta
from nexapilot.llm.routing import ModelRouter
from nexapilot.loop.interrupt import InterruptManager
from nexapilot.loop.session_loop import SessionLoop
from nexapilot.model import Message, ModelRef, PermissionRule, Session, TextPart
from nexapilot.permission.service import PermissionService
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.tools.registry import ToolRegistry


class _ProviderHTTPError(RuntimeError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"code": "test_error", "message": message}}
        self.response = SimpleNamespace(headers={})


class _ScriptedProvider:
    def __init__(self, scripts: list[list[LLMEvent | BaseException]]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[LLMEvent]:
        self.calls.append(kwargs)
        script = self.scripts.pop(0)
        for action in script:
            if isinstance(action, BaseException):
                raise action
            yield action


class ModelGatewayEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_style_message_completes_through_backup_provider(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "gateway-e2e.sqlite3")
            cfg = _build_config(
                {
                    "openai": {
                        "base_url": "https://bootstrap.example/v1",
                        "api_key": "bootstrap",
                        "model": "bootstrap-model",
                        "transport": "responses",
                        "reasoning_effort": "medium",
                        "resilience": {
                            "retry": {"max_attempts": 1},
                            "circuit_breaker": {"enabled": False},
                        },
                    },
                    "model_gateway": {
                        "enabled": True,
                        "default_route": "coding",
                        "providers": {
                            "primary": {
                                "type": "openai",
                                "base_url": "https://primary.example/v1",
                                "api_key_env": "PRIMARY_KEY",
                                "transports": ["responses"],
                                "capability_profile": "openai",
                            },
                            "backup": {
                                "type": "openai_compatible",
                                "base_url": "https://backup.example/v1",
                                "api_key_env": "BACKUP_KEY",
                                "transports": ["responses"],
                                "capability_profile": "openai_compatible",
                            },
                        },
                        "models": {
                            "premium": {
                                "provider": "primary",
                                "model": "premium-model",
                                "transport": "responses",
                            },
                            "balanced": {
                                "provider": "backup",
                                "model": "balanced-model",
                                "transport": "responses",
                            },
                        },
                        "routes": {
                            "coding": {
                                "candidates": ["premium", "balanced"],
                                "fallback_on": ["rate_limit"],
                                "max_fallback_hops": 1,
                                "max_total_attempts": 2,
                            }
                        },
                    },
                    "db_path": db_path,
                    "default_worktree": tmp,
                    "system_prompt": "You are a personal agent.",
                }
            )
            store = SQLiteStore(db_path)
            await store.init()
            now = int(time.time() * 1000)
            session = Session(
                id="session-e2e",
                title="gateway e2e",
                worktree=tmp,
                cwd=tmp,
                created_at=now,
                updated_at=now,
                permission_rules=[
                    PermissionRule(permission="*", pattern="*", action="allow")
                ],
            )
            await store.create_session(session)
            user = Message(
                id="user-e2e",
                session_id=session.id,
                role="user",
                parent_id=None,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="bootstrap-model"),
                created_at=now,
            )
            await store.add_message(user)
            await store.add_part(
                session.id,
                user.id,
                TextPart(
                    id="user-part-e2e",
                    message_id=user.id,
                    session_id=session.id,
                    text="只回答：网关端到端验证通过",
                ),
            )

            primary = _ScriptedProvider(
                [[_ProviderHTTPError("primary throttled", status_code=429)]]
            )
            backup = _ScriptedProvider(
                [
                    [
                        ResponseStarted("response_started", "backup-request"),
                        TextDelta("text_delta", "网关端到端验证通过"),
                        Finish("finish", "stop"),
                    ]
                ]
            )
            bus = Bus()
            gateway = ProviderGateway(
                config=cfg.openai,
                store=store,
                adapters={
                    "primary:responses": primary,
                    "backup:responses": backup,
                },
                bus=bus,
                router=ModelRouter(cfg.model_gateway, cfg.openai),
            )
            loop = SessionLoop(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=PermissionService(bus, store),
                tools=ToolRegistry([]),
                llm=gateway,
                interrupt=InterruptManager(),
            )

            assistant_id, _trace_id = await loop.run(session_id=session.id)

            messages = await store.list_messages(session.id)
            assistant = next(
                message for message in messages if message.info.id == assistant_id
            )
            self.assertEqual(
                [part.text for part in assistant.parts if part.type == "text"],
                ["网关端到端验证通过"],
            )
            runs = await store.list_runs(session.id)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].status, "completed")
            calls = await store.list_run_llm_calls(runs[0].id)
            self.assertEqual(
                [(call["provider"], call["model"], call["status"]) for call in calls],
                [
                    ("primary", "premium-model", "failed"),
                    ("backup", "balanced-model", "completed"),
                ],
            )
            self.assertEqual(calls[1]["fallback_from_call_id"], calls[0]["id"])
            self.assertEqual(len(primary.calls), 1)
            self.assertEqual(len(backup.calls), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from nexapilot.config import (  # noqa: E402
    ChannelsConfig,
    Config,
    DaytonaConfig,
    FeishuChannelConfig,
    HooksConfig,
    KBConfig,
    LangfuseConfig,
    LoggingConfig,
    MemoryContextManagerConfig,
    MemoryConfig,
    MemoryFeatureConfig,
    MemoryProcessingConfig,
    OpenAIConfig,
    VLMConfig,
    WebSearchConfig,
)
from nexapilot.hookdefs import Hook  # noqa: E402
from nexapilot.hooks import Hooker  # noqa: E402
from nexapilot.memory.hooks import create_memory_hooks  # noqa: E402
from nexapilot.memory.service import MemoryService  # noqa: E402
from nexapilot.model import PermissionRule, Session, SessionRuntime  # noqa: E402
from nexapilot.store.sqlite import SQLiteStore  # noqa: E402


def make_config(
    *,
    db_path: str,
    worktree: str,
    structured: bool = False,
    shadow_mode: bool = True,
) -> Config:
    return Config(
        openai=OpenAIConfig(base_url="http://local", api_key="k", model="m"),
        langfuse=LangfuseConfig(
            enabled=False,
            public_key="",
            secret_key="",
            base_url="https://cloud.langfuse.com",
            environment="test",
            sample_rate=1.0,
            debug=False,
        ),
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=False,
                app_id="",
                app_secret="",
                encrypt_key="",
                verification_token="",
                allow_from=[],
            )
        ),
        kb=KBConfig(backend="none", base_url="", api_key=""),
        vlm=VLMConfig(backend="none", api_url="", api_key="", poll_interval=5, timeout=1800),
        logging=LoggingConfig(level="INFO", console=False, file=False, dir="./data/logs", rotation="00:00", retention="7 days"),
        hooks=HooksConfig(debug=False),
        web_search=WebSearchConfig(tavily_api_key=""),
        system_prompt="sys",
        db_path=db_path,
        default_worktree=worktree,
        default_permission_action="ask",
        prompt_templates={},
        memory=MemoryConfig(
            enabled=True,
            processing=MemoryProcessingConfig(enabled=structured),
            semantic=MemoryFeatureConfig(enabled=structured),
            context_manager=MemoryContextManagerConfig(
                enabled=structured,
                shadow_mode=shadow_mode,
            ),
        ),
        daytona=DaytonaConfig(),
    )


class MemoryHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_session_injects_memory_and_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "memory.md").write_text("x" * 8100, encoding="utf-8")
            db_path = str(worktree / "app.sqlite3")
            cfg = make_config(db_path=db_path, worktree=str(worktree))
            store = SQLiteStore(db_path)
            await store.init()
            session = Session(
                id="s1",
                title="local",
                worktree=str(worktree),
                cwd=str(worktree),
                created_at=1,
                updated_at=1,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                runtime=SessionRuntime(backend="local"),
            )
            await store.create_session(session)
            service = MemoryService(cfg=cfg, store=store, embedding_provider_factory=lambda _cfg: None)
            hooker = Hooker()
            hooker.add(create_memory_hooks(cfg=cfg, store=store, service=service))

            output = {"system": ["base system"]}
            await hooker.trigger(
                Hook.ExperimentalChatSystemTransform,
                {"session_id": "s1", "agent": "primary", "model": {"id": "m"}},
                output,
            )

            rendered = "\n\n".join(output["system"])
            self.assertIn("## Workspace Memory", rendered)
            self.assertIn("[memory.md truncated to 8000 characters]", rendered)
            self.assertIn("Before answering questions about prior work", rendered)

    async def test_daytona_session_does_not_inject_memory_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "memory.md").write_text("should not load", encoding="utf-8")
            db_path = str(worktree / "app.sqlite3")
            cfg = make_config(db_path=db_path, worktree=str(worktree))
            store = SQLiteStore(db_path)
            await store.init()
            session = Session(
                id="s2",
                title="remote",
                worktree=str(worktree),
                cwd=str(worktree),
                created_at=1,
                updated_at=1,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                runtime=SessionRuntime(backend="daytona"),
            )
            await store.create_session(session)
            service = MemoryService(cfg=cfg, store=store, embedding_provider_factory=lambda _cfg: None)
            hooker = Hooker()
            hooker.add(create_memory_hooks(cfg=cfg, store=store, service=service))

            output = {"system": ["base system"]}
            await hooker.trigger(
                Hook.ExperimentalChatSystemTransform,
                {"session_id": "s2", "agent": "primary", "model": {"id": "m"}},
                output,
            )

            self.assertEqual(output["system"], ["base system"])

    async def test_structured_shadow_preserves_baseline_without_prompting_dual_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "memory.md").write_text("legacy baseline", encoding="utf-8")
            db_path = str(worktree / "app.sqlite3")
            cfg = make_config(
                db_path=db_path,
                worktree=str(worktree),
                structured=True,
                shadow_mode=True,
            )
            store = SQLiteStore(db_path)
            await store.init()
            await store.create_session(
                Session(
                    id="s3",
                    title="shadow",
                    worktree=str(worktree),
                    cwd=str(worktree),
                    created_at=1,
                    updated_at=1,
                    permission_rules=[],
                )
            )
            service = MemoryService(cfg=cfg, store=store, embedding_provider_factory=lambda _cfg: None)
            output = {"system": ["base system"]}

            await create_memory_hooks(cfg=cfg, store=store, service=service)[
                Hook.ExperimentalChatSystemTransform
            ]({"session_id": "s3"}, output)

            rendered = "\n".join(output["system"])
            self.assertIn("legacy baseline", rendered)
            self.assertIn("projected into structured memory", rendered)
            self.assertIn("do not rewrite them merely", rendered)
            self.assertNotIn("When you need to update long-term memory, rewrite memory.md", rendered)

    async def test_active_structured_context_does_not_auto_inject_memory_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "memory.md").write_text("legacy file should be recalled on demand", encoding="utf-8")
            db_path = str(worktree / "app.sqlite3")
            cfg = make_config(
                db_path=db_path,
                worktree=str(worktree),
                structured=True,
                shadow_mode=False,
            )
            store = SQLiteStore(db_path)
            await store.init()
            await store.create_session(
                Session(
                    id="s4",
                    title="active",
                    worktree=str(worktree),
                    cwd=str(worktree),
                    created_at=1,
                    updated_at=1,
                    permission_rules=[],
                )
            )
            service = MemoryService(cfg=cfg, store=store, embedding_provider_factory=lambda _cfg: None)
            output = {"system": ["base system"]}

            await create_memory_hooks(cfg=cfg, store=store, service=service)[
                Hook.ExperimentalChatSystemTransform
            ]({"session_id": "s4"}, output)

            rendered = "\n".join(output["system"])
            self.assertNotIn("## Workspace Memory", rendered)
            self.assertNotIn("legacy file should be recalled on demand", rendered)
            self.assertIn("explicit user-managed reference files", rendered)

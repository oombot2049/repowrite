from __future__ import annotations

from pathlib import Path

from nexapilot.config import Config
from nexapilot.hookdefs import Hook
from nexapilot.memory.service import MemoryService
from nexapilot.store.sqlite import SQLiteStore

_MAX_MEMORY_CONTEXT_CHARS = 8_000


def create_memory_hooks(*, cfg: Config, store: SQLiteStore, service: MemoryService):
    async def on_system_transform(input: dict, output: dict) -> None:
        if not service.enabled:
            return
        session_id = str(input.get("session_id") or "").strip()
        if not session_id:
            return
        try:
            session = await store.get_session(session_id)
        except KeyError:
            return
        if session.runtime.backend != "local":
            return

        system_parts = output.get("system")
        if not isinstance(system_parts, list):
            return

        structured_pipeline = (
            cfg.memory.processing.enabled
            and cfg.memory.semantic.enabled
        )
        active_structured_context = (
            structured_pipeline
            and cfg.memory.context_manager.enabled
            and not cfg.memory.context_manager.shadow_mode
        )
        worktree = Path(session.memory_worktree)
        memory_file = worktree / "memory.md"
        if memory_file.is_file() and not active_structured_context:
            content = memory_file.read_text(encoding="utf-8")
            truncated = False
            if len(content) > _MAX_MEMORY_CONTEXT_CHARS:
                content = content[:_MAX_MEMORY_CONTEXT_CHARS]
                truncated = True
            section = ["## Workspace Memory", content]
            if truncated:
                section.append("[memory.md truncated to 8000 characters]")
            system_parts.append("\n".join(section))

        guidance = [
            "## Memory Recall",
            "Before answering questions about prior work, decisions, preferences, or todos, use memory_search on local memory.",
            "Use memory_get only after memory_search when you need exact lines or larger raw context.",
        ]
        if structured_pipeline:
            guidance.extend(
                [
                    "Explicit durable user facts, preferences, goals, and constraints are projected into structured memory after the Run completes.",
                    "Treat memory.md and memory/**/*.md as explicit user-managed reference files; do not rewrite them merely because the user says to remember something.",
                    "Only update those files when the user explicitly requests file-managed memory or dated notes.",
                ]
            )
        else:
            guidance.extend(
                [
                    "When you need to update long-term memory, rewrite memory.md or append dated notes to memory/YYYY-MM-DD.md using the existing read and write tools.",
                    "Put stable evergreen facts in memory.md, and append dated session conclusions to memory/YYYY-MM-DD.md.",
                ]
            )
        system_parts.append("\n".join(guidance))

    return {Hook.ExperimentalChatSystemTransform: on_system_transform}

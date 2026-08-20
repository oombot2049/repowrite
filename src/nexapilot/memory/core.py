from __future__ import annotations

import hashlib
from html import escape
from pathlib import Path

from nexapilot.model import CoreMemoryBlock, MessageWithParts, OutboxEvent, SemanticMemory
from nexapilot.store.sqlite import SQLiteStore


class CoreMemoryBuilder:
    _TYPE_TO_BLOCK = {
        "profile": "project_profile",
        "fact": "project_profile",
        "preference": "user_preferences",
        "constraint": "active_constraints",
        "goal": "active_goals",
        "decision": "critical_decisions",
        "lesson": "critical_decisions",
    }
    _PRIORITY = {
        "active_constraints": 100,
        "active_goals": 90,
        "critical_decisions": 80,
        "user_preferences": 70,
        "project_profile": 60,
    }

    def __init__(self, *, store: SQLiteStore, max_tokens: int = 1_200) -> None:
        self._store = store
        self._max_tokens = max(100, max_tokens)

    async def rebuild(self, workspace: str, *, namespace: str = "project", now_ms: int) -> list[CoreMemoryBlock]:
        memories = await self._store.list_semantic_memories(
            workspace,
            namespace=namespace,
            status="active",
            limit=10_000,
        )
        grouped: dict[str, list[SemanticMemory]] = {}
        for memory in memories:
            block_type = self._TYPE_TO_BLOCK[memory.memory_type]
            grouped.setdefault(block_type, []).append(memory)

        remaining_chars = self._max_tokens * 4
        blocks: list[CoreMemoryBlock] = []
        for block_type in sorted(grouped, key=lambda item: self._PRIORITY[item], reverse=True):
            lines: list[str] = []
            source_ids: list[str] = []
            for memory in grouped[block_type]:
                line = f"- {memory.subject} {memory.predicate}: {memory.value}"
                required = len(line) + (1 if lines else 0)
                if required > remaining_chars:
                    continue
                lines.append(line)
                source_ids.append(memory.id)
                remaining_chars -= required
            if not lines:
                continue
            content = "\n".join(lines)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            block_id = hashlib.sha256(f"{workspace}\n{namespace}\n{block_type}".encode("utf-8")).hexdigest()
            blocks.append(
                CoreMemoryBlock(
                    id=block_id,
                    workspace=workspace,
                    namespace=namespace,
                    block_type=block_type,
                    content=content,
                    source_memory_ids=source_ids,
                    priority=self._PRIORITY[block_type],
                    token_count=max(1, (len(content) + 3) // 4),
                    content_hash=content_hash,
                    created_at=now_ms,
                    updated_at=now_ms,
                )
            )
        return await self._store.replace_core_memory_blocks(workspace, namespace, blocks)

    @staticmethod
    def render(blocks: list[CoreMemoryBlock]) -> str:
        if not blocks:
            return ""
        lines = ["<core_memory>"]
        for block in blocks:
            lines.append(f"  <{block.block_type}>")
            lines.extend(f"    {escape(line)}" for line in block.content.splitlines())
            lines.append(f"  </{block.block_type}>")
        lines.append("</core_memory>")
        return "\n".join(lines)


class CoreMemoryProjector:
    def __init__(self, *, store: SQLiteStore, max_tokens: int) -> None:
        self._store = store
        self._builder = CoreMemoryBuilder(store=store, max_tokens=max_tokens)

    async def __call__(self, event: OutboxEvent, _messages: list[MessageWithParts]) -> None:
        if not event.session_id:
            raise ValueError("core memory projection requires a session identifier")
        session = await self._store.get_session(event.session_id)
        workspace = str(Path(session.memory_worktree).resolve())
        await self._builder.rebuild(workspace, now_ms=event.created_at)

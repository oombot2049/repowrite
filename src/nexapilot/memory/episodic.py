from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from nexapilot.model import Episode, MessageWithParts, OutboxEvent, TextPart, ToolPart
from nexapilot.store.sqlite import SQLiteStore


class EpisodicProjector:
    """Build a deterministic, rebuildable Episode from one completed agent Run."""

    EXTRACTOR_VERSION = "episodic-rules-v1"
    _MUTATING_TOOLS = {"write", "edit", "apply_patch", "multiedit", "notebookedit"}

    def __init__(self, *, store: SQLiteStore) -> None:
        self._store = store

    async def __call__(self, event: OutboxEvent, messages: list[MessageWithParts]) -> None:
        if not event.run_id or not event.session_id:
            raise ValueError("episodic projection requires run and session identifiers")
        if event.sequence_from is None or event.sequence_to is None:
            raise ValueError("episodic projection requires a message sequence range")

        run = await self._store.get_run(event.run_id)
        session = await self._store.get_session(event.session_id)
        run_messages = [message for message in messages if message.info.run_id == event.run_id]
        if not run_messages:
            raise RuntimeError(f"no messages found for completed run: {event.run_id}")

        user_texts = self._texts(message for message in run_messages if message.info.role == "user")
        assistant_texts = self._texts(message for message in run_messages if message.info.role == "assistant")
        actions, errors, artifacts = self._tool_facts(run_messages)
        if run.error:
            error_text = str(run.error.get("message") or run.error.get("type") or "run failed")
            errors.append(self._clean(error_text, limit=500))

        goal = self._clean(user_texts[0] if user_texts else session.title, limit=1_000)
        lessons = [self._clean(assistant_texts[-1], limit=1_000)] if assistant_texts else []
        episode = Episode(
            id=event.run_id,
            workspace=str(Path(session.memory_worktree).resolve()),
            source_session_id=event.session_id,
            source_run_id=event.run_id,
            source_kind=session.kind,
            source_agent=session.agent_name,
            sequence_from=event.sequence_from,
            sequence_to=event.sequence_to,
            goal=goal,
            actions=self._deduplicate(actions, limit=30),
            outcome=run.status,
            errors=self._deduplicate(errors, limit=20),
            artifacts=self._deduplicate(artifacts, limit=50),
            lessons=self._deduplicate(lessons, limit=5),
            extractor_version=self.EXTRACTOR_VERSION,
            created_at=run.created_at,
            updated_at=run.completed_at or run.updated_at,
        )
        await self._store.upsert_episode(episode)

    @classmethod
    def _texts(cls, messages: Iterable[MessageWithParts]) -> list[str]:
        texts: list[str] = []
        for message in messages:
            joined = "".join(part.text for part in message.parts if isinstance(part, TextPart)).strip()
            if joined:
                texts.append(joined)
        return texts

    @classmethod
    def _tool_facts(cls, messages: list[MessageWithParts]) -> tuple[list[str], list[str], list[str]]:
        actions: list[str] = []
        errors: list[str] = []
        artifacts: list[str] = []
        for message in messages:
            for part in message.parts:
                if not isinstance(part, ToolPart):
                    continue
                state = part.state
                title = getattr(state, "title", None) or part.tool
                status = state.status
                actions.append(cls._clean(f"{part.tool}: {title} ({status})", limit=300))
                if status == "error":
                    errors.append(cls._clean(getattr(state, "error", "tool failed"), limit=500))
                elif status == "completed" and bool(state.metadata.get("error")):
                    errors.append(cls._clean(f"{part.tool}: {title} failed", limit=500))
                if part.tool.lower() in cls._MUTATING_TOOLS:
                    for source in (getattr(state, "input", {}), getattr(state, "metadata", {})):
                        for key in ("file_path", "path"):
                            value = source.get(key) if isinstance(source, dict) else None
                            if isinstance(value, str) and value.strip():
                                artifacts.append(cls._clean(value, limit=500))
        return actions, errors, artifacts

    @staticmethod
    def _deduplicate(values: list[str], *, limit: int) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))[:limit]

    @staticmethod
    def _clean(value: str, *, limit: int) -> str:
        compact = " ".join(value.split())
        compact = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_API_KEY]", compact)
        compact = re.sub(
            r"(?i)\b(authorization|api[_-]?key|token)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            compact,
        )
        return compact if len(compact) <= limit else compact[: limit - 3] + "..."

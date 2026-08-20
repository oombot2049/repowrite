from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path

from nexapilot.config import MemoryContextManagerConfig
from nexapilot.memory.core import CoreMemoryBuilder
from nexapilot.model import (
    MessageWithParts,
    ProviderStatePart,
    ReasoningPart,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
    ToolStateError,
    ToolStatePending,
    ToolStateRunning,
)
from nexapilot.store.sqlite import SQLiteStore


@dataclass(frozen=True)
class ContextResult:
    history: list[MessageWithParts]
    memory_context: str
    stats: dict[str, int | bool]


class ContextManager:
    """Select recent atomic Run units and retrieve long-term memory under one budget."""

    def __init__(self, *, store: SQLiteStore, config: MemoryContextManagerConfig) -> None:
        self._store = store
        self._config = config

    @property
    def shadow_mode(self) -> bool:
        return self._config.shadow_mode

    @property
    def max_input_tokens(self) -> int:
        return self._config.max_input_tokens

    @property
    def overflow_retry_input_tokens(self) -> int:
        """Return a single, stricter budget for provider overflow recovery."""
        return max(
            self._config.reserved_output_tokens + 1,
            min(
                self._config.max_input_tokens - 1,
                int(self._config.max_input_tokens * 0.7),
            ),
        )

    async def build(
        self,
        *,
        session: Session,
        history: list[MessageWithParts],
        system_text: str,
        max_input_tokens: int | None = None,
    ) -> ContextResult:
        effective_max_input_tokens = max_input_tokens or self._config.max_input_tokens
        if effective_max_input_tokens <= self._config.reserved_output_tokens:
            raise ValueError(
                "max_input_tokens must be greater than reserved_output_tokens"
            )
        workspace = str(Path(session.memory_worktree).resolve())
        query = self._latest_user_text(history)
        blocks = await self._store.list_core_memory_blocks(workspace)
        semantic_hits = (
            await self._store.search_semantic_memories(
                workspace,
                query,
                limit=self._config.max_semantic_results,
            )
            if query and self._config.max_semantic_results > 0
            else []
        )
        episode_hits = (
            await self._store.search_episodes(
                workspace,
                query,
                limit=self._config.max_episodic_results,
                subagent_weight=self._config.subagent_episode_weight,
            )
            if query and self._config.max_episodic_results > 0
            else []
        )
        memory_context = self._render_memory(blocks, semantic_hits, episode_hits)
        system_tokens = self.estimate_tokens(system_text)
        memory_budget = max(
            0,
            effective_max_input_tokens
            - self._config.reserved_output_tokens
            - system_tokens,
        )
        memory_tokens = self.estimate_tokens(memory_context)
        memory_truncated = memory_tokens > memory_budget
        if memory_truncated:
            memory_context = self._truncate_text(memory_context, memory_budget * 4)
            memory_tokens = self.estimate_tokens(memory_context)
        fixed_tokens = system_tokens + memory_tokens
        history_budget = max(
            1,
            effective_max_input_tokens
            - self._config.reserved_output_tokens
            - fixed_tokens,
        )
        selected = self._select_recent_units(history, history_budget)
        selected_tokens = sum(self._message_tokens(message) for message in selected)
        input_tokens = fixed_tokens + selected_tokens
        budget_overflow_tokens = max(
            0,
            input_tokens
            + self._config.reserved_output_tokens
            - effective_max_input_tokens,
        )
        return ContextResult(
            history=selected,
            memory_context=memory_context,
            stats={
                "shadow_mode": self._config.shadow_mode,
                "history_messages_total": len(history),
                "history_messages_selected": len(selected),
                "history_tokens_estimated": selected_tokens,
                "fixed_tokens_estimated": fixed_tokens,
                "input_tokens_estimated": input_tokens,
                "max_input_tokens": effective_max_input_tokens,
                "history_budget_tokens": history_budget,
                "budget_overflow_tokens": budget_overflow_tokens,
                "memory_truncated": memory_truncated,
                "core_blocks": len(blocks),
                "semantic_hits": len(semantic_hits),
                "episodic_hits": len(episode_hits),
            },
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    @classmethod
    def _message_tokens(cls, message: MessageWithParts) -> int:
        payload = json.dumps(
            [part.model_dump() for part in message.parts],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return max(4, cls.estimate_tokens(payload) + 4)

    @classmethod
    def _select_recent_units(
        cls,
        history: list[MessageWithParts],
        budget_tokens: int,
    ) -> list[MessageWithParts]:
        units: list[list[MessageWithParts]] = []
        run_unit_indexes: dict[str, int] = {}
        for message in history:
            run_id = message.info.run_id
            if run_id is None:
                units.append([message])
                continue
            unit_index = run_unit_indexes.get(run_id)
            if unit_index is None:
                run_unit_indexes[run_id] = len(units)
                units.append([message])
            else:
                units[unit_index].append(message)

        selected_units: list[list[MessageWithParts]] = []
        used = 0
        for unit in reversed(units):
            unit_tokens = sum(cls._message_tokens(message) for message in unit)
            remaining = budget_tokens - used
            if unit_tokens > remaining:
                unit = cls._truncate_unit(unit, remaining)
                unit_tokens = sum(cls._message_tokens(message) for message in unit)
            if unit_tokens > remaining or not unit:
                continue
            selected_units.append(unit)
            used += unit_tokens
            if used >= budget_tokens:
                break
        selected_units.reverse()
        return [message for unit in selected_units for message in unit]

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        marker = "\n…[truncated by Context Manager]"
        if max_chars <= len(marker):
            return marker[:max_chars]
        return value[: max_chars - len(marker)] + marker

    @classmethod
    def _truncate_mapping(cls, value, max_chars: int):
        if isinstance(value, str):
            return cls._truncate_text(value, max_chars)
        if isinstance(value, list):
            return [cls._truncate_mapping(item, max_chars) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._truncate_mapping(item, max_chars)
                for key, item in value.items()
            }
        return value

    @classmethod
    def _truncate_unit(
        cls,
        unit: list[MessageWithParts],
        budget_tokens: int,
    ) -> list[MessageWithParts]:
        if budget_tokens <= 0:
            return []
        for max_chars in (16_000, 8_000, 4_000, 2_000, 1_000, 500, 200, 80):
            candidate = cls._truncate_parts(unit, max_chars=max_chars)
            if sum(cls._message_tokens(message) for message in candidate) <= budget_tokens:
                return candidate
        return cls._compact_tool_cycles(unit, budget_tokens)

    @classmethod
    def _truncate_parts(
        cls,
        unit: list[MessageWithParts],
        *,
        max_chars: int,
    ) -> list[MessageWithParts]:
        candidate = [message.model_copy(deep=True) for message in unit]
        for message in candidate:
            for part in message.parts:
                if isinstance(part, (TextPart, ReasoningPart)):
                    part.text = cls._truncate_text(part.text, max_chars)
                elif isinstance(part, ProviderStatePart):
                    part.data = cls._truncate_mapping(part.data, max_chars)
                elif isinstance(part, ToolPart):
                    state = part.state
                    if isinstance(state, ToolStatePending):
                        state.raw = cls._truncate_text(state.raw, max_chars)
                    if isinstance(
                        state,
                        (
                            ToolStatePending,
                            ToolStateRunning,
                            ToolStateCompleted,
                            ToolStateError,
                        ),
                    ):
                        state.input = cls._truncate_mapping(state.input, max_chars)
                    if isinstance(state, ToolStateCompleted):
                        state.output = cls._truncate_text(state.output, max_chars)
                        state.metadata = cls._truncate_mapping(
                            state.metadata, max_chars
                        )
                    elif isinstance(state, ToolStateError):
                        state.error = cls._truncate_text(state.error, max_chars)
                        if state.metadata is not None:
                            state.metadata = cls._truncate_mapping(
                                state.metadata, max_chars
                            )
                    elif (
                        isinstance(state, ToolStateRunning)
                        and state.metadata is not None
                    ):
                        state.metadata = cls._truncate_mapping(
                            state.metadata, max_chars
                        )
        return candidate

    @classmethod
    def _compact_tool_cycles(
        cls,
        unit: list[MessageWithParts],
        budget_tokens: int,
    ) -> list[MessageWithParts]:
        """Keep the newest complete tool cycles when a long Run exceeds budget.

        A Run is normally selected atomically so function calls never lose their
        outputs. Long-running agents can still accumulate dozens of calls whose
        structural overhead cannot be fixed by truncating strings. In that case
        retain the user request plus as many newest call/output pairs as fit.
        Provider State is deliberately removed because it cannot be replayed
        safely after earlier items from the same provider response are pruned.
        """
        call_ids = [
            str(message.info.tool_call_id)
            for message in unit
            if message.info.role == "tool" and message.info.tool_call_id
        ]
        if not call_ids:
            return []

        for keep_count in range(len(call_ids) - 1, -1, -1):
            kept_call_ids = set(call_ids[-keep_count:]) if keep_count else set()
            compacted: list[MessageWithParts] = []
            for original in unit:
                if (
                    original.info.role == "tool"
                    and original.info.tool_call_id not in kept_call_ids
                ):
                    continue
                message = original.model_copy(deep=True)
                if message.info.role == "assistant":
                    message.parts = [
                        part
                        for part in message.parts
                        if not isinstance(part, ProviderStatePart)
                        and (
                            not isinstance(part, ToolPart)
                            or part.call_id in kept_call_ids
                        )
                    ]
                compacted.append(message)

            for max_chars in (2_000, 1_000, 500, 200, 80):
                candidate = cls._truncate_parts(
                    compacted,
                    max_chars=max_chars,
                )
                if (
                    candidate
                    and sum(cls._message_tokens(message) for message in candidate)
                    <= budget_tokens
                ):
                    return candidate
        return []

    @staticmethod
    def _latest_user_text(history: list[MessageWithParts]) -> str:
        for message in reversed(history):
            if message.info.role != "user":
                continue
            return "".join(part.text for part in message.parts if isinstance(part, TextPart)).strip()
        return ""

    @staticmethod
    def _render_memory(blocks, semantic_hits, episode_hits) -> str:
        sections: list[str] = []
        core = CoreMemoryBuilder.render(blocks)
        if core:
            sections.append(core)
        core_source_ids = {
            source_id
            for block in blocks
            for source_id in block.source_memory_ids
        }
        remaining_semantic_hits = [
            hit for hit in semantic_hits if hit.memory.id not in core_source_ids
        ]
        if remaining_semantic_hits:
            lines = ["<semantic_memory>"]
            for hit in remaining_semantic_hits:
                memory = hit.memory
                lines.append(
                    "  <fact type=\"{}\" subject=\"{}\" predicate=\"{}\">{}</fact>".format(
                        escape(memory.memory_type, quote=True),
                        escape(memory.subject, quote=True),
                        escape(memory.predicate, quote=True),
                        escape(memory.value),
                    )
                )
            lines.append("</semantic_memory>")
            sections.append("\n".join(lines))
        if episode_hits:
            lines = ["<episodic_memory>"]
            for hit in episode_hits:
                episode = hit.episode
                summary = episode.lessons[-1] if episode.lessons else episode.outcome
                lines.append(
                    f"  <episode outcome=\"{escape(episode.outcome, quote=True)}\" "
                    f"source=\"{escape(episode.source_kind, quote=True)}\">"
                    f"{escape(episode.goal)} — {escape(summary)}</episode>"
                )
            lines.append("</episodic_memory>")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from nexapilot.model import MessageWithParts, OutboxEvent, SemanticMemory, TextPart
from nexapilot.store.sqlite import SQLiteStore


@dataclass(frozen=True)
class SemanticCandidate:
    memory_type: str
    subject: str
    predicate: str
    value: str
    confidence: float
    importance: float
    source_message_id: str


class RuleBasedSemanticExtractor:
    """Conservative baseline: extract only explicit, durable user statements."""

    _SECRET = re.compile(
        r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\b(?:api[_-]?key|authorization|token)\s*[:=]\s*\S+)"
    )

    def extract(self, messages: list[MessageWithParts]) -> list[SemanticCandidate]:
        candidates: list[SemanticCandidate] = []
        for message in messages:
            if message.info.role != "user":
                continue
            text = "".join(part.text for part in message.parts if isinstance(part, TextPart))
            for sentence in self._sentences(text):
                if self._SECRET.search(sentence):
                    continue
                candidate = self._classify(sentence, message.info.id)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [part.strip() for part in re.split(r"[\n。！？!?]+", text) if part.strip()]

    @staticmethod
    def _candidate(
        memory_type: str,
        subject: str,
        predicate: str,
        value: str,
        source_message_id: str,
        *,
        confidence: float = 0.95,
        importance: float = 0.8,
    ) -> SemanticCandidate:
        return SemanticCandidate(
            memory_type=memory_type,
            subject=subject,
            predicate=predicate,
            value=" ".join(value.split()),
            confidence=confidence,
            importance=importance,
            source_message_id=source_message_id,
        )

    def _classify(self, sentence: str, message_id: str) -> SemanticCandidate | None:
        stage = re.search(r"(?i)\b(P\d+)\b", sentence)
        if stage and re.search(r"先不用|暂停|暂缓|不做|pause|hold|defer", sentence, re.I):
            return self._candidate("constraint", stage.group(1).upper(), "status", "paused", message_id, importance=0.95)
        if stage and re.search(r"开始|继续|恢复|启动|resume|start|continue", sentence, re.I):
            return self._candidate("constraint", stage.group(1).upper(), "status", "active", message_id, importance=0.95)

        project_name = re.search(
            r"(?i)(?:就叫|项目名称(?:是|为)|project\s+name\s+is)\s*[“\"']?([A-Za-z][\w-]{1,63})",
            sentence,
        )
        if project_name:
            return self._candidate("decision", "project", "name", project_name.group(1), message_id, importance=0.95)

        explicit_remember = bool(
            re.match(r"(?i)^(?:请记住|记住|remember\s+that)\s*[:：]?", sentence)
        )
        value = sentence
        if explicit_remember:
            value = re.sub(
                r"(?i)^(?:请记住|记住|remember\s+that)\s*[:：]?\s*",
                "",
                sentence,
            ).strip()

        if re.search(
            r"我偏好|我的偏好|以后请|今后请|I\s+prefer|from\s+now\s+on|"
            r"(?:我|我的).*(?:优先|习惯|倾向|希望)",
            value,
            re.I,
        ):
            return self._candidate("preference", "user", "workflow", value, message_id)
        if re.search(r"必须|不要|禁止|始终|永远|务必|must|never|always|do\s+not", value, re.I):
            return self._candidate("constraint", "project", "operating_rule", value, message_id)
        if explicit_remember and re.search(
            r"(?:我的)?(?:目标|计划)|(?:我要|我准备)|my\s+goal|I\s+plan",
            value,
            re.I,
        ):
            return self._candidate("goal", "user", "active_goal", value, message_id, importance=0.9)
        if explicit_remember:
            return self._candidate("fact", "project", "remembered_fact", value, message_id, confidence=0.9)
        return None


class SemanticProjector:
    EXTRACTOR_VERSION = "semantic-rules-v1"
    SUBAGENT_EXTRACTOR_VERSION = "subagent-candidate-v1"

    def __init__(self, *, store: SQLiteStore, extractor: RuleBasedSemanticExtractor | None = None) -> None:
        self._store = store
        self._extractor = extractor or RuleBasedSemanticExtractor()

    async def __call__(self, event: OutboxEvent, messages: list[MessageWithParts]) -> None:
        if not event.session_id or not event.run_id:
            raise ValueError("semantic projection requires run and session identifiers")
        session = await self._store.get_session(event.session_id)
        run_messages = [message for message in messages if message.info.run_id == event.run_id]
        workspace = str(Path(session.memory_worktree).resolve())
        now = event.created_at
        if session.kind == "subagent":
            candidate = self._subagent_conclusion(run_messages)
            if candidate is None:
                return
            value, source_message_id = candidate
            content = "\n".join(
                ["lesson", f"run:{event.run_id}", "subagent_finding", value]
            ).casefold()
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            memory = SemanticMemory(
                id=f"candidate-{hashlib.sha256(event.run_id.encode()).hexdigest()[:32]}",
                namespace="project",
                workspace=workspace,
                memory_type="lesson",
                subject=f"run:{event.run_id}",
                predicate="subagent_finding",
                value=value,
                status="candidate",
                confidence=0.6,
                importance=0.5,
                source_session_id=event.session_id,
                source_run_id=event.run_id,
                source_kind="subagent",
                source_agent=session.agent_name,
                source_message_ids=[source_message_id],
                content_hash=content_hash,
                version=1,
                extractor_version=self.SUBAGENT_EXTRACTOR_VERSION,
                created_at=now,
                updated_at=now,
            )
            await self._store.put_semantic_candidate_once(memory)
            return

        for candidate in self._extractor.extract(run_messages):
            content = "\n".join(
                [candidate.memory_type, candidate.subject, candidate.predicate, candidate.value]
            ).casefold()
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            memory = SemanticMemory(
                id=str(uuid4()),
                namespace="project",
                workspace=workspace,
                memory_type=candidate.memory_type,
                subject=candidate.subject,
                predicate=candidate.predicate,
                value=candidate.value,
                status="active",
                confidence=candidate.confidence,
                importance=candidate.importance,
                valid_from=now,
                source_session_id=event.session_id,
                source_run_id=event.run_id,
                source_kind="primary",
                source_agent=session.agent_name,
                source_message_ids=[candidate.source_message_id],
                content_hash=content_hash,
                version=1,
                extractor_version=self.EXTRACTOR_VERSION,
                created_at=now,
                updated_at=now,
            )
            await self._store.activate_semantic_memory(memory)

    @classmethod
    def _subagent_conclusion(
        cls,
        messages: list[MessageWithParts],
    ) -> tuple[str, str] | None:
        for message in reversed(messages):
            if message.info.role != "assistant":
                continue
            text = "".join(
                part.text for part in message.parts if isinstance(part, TextPart)
            ).strip()
            if not text:
                continue
            value = cls._clean(text, limit=1_500)
            return (value, message.info.id) if value else None
        return None

    @staticmethod
    def _clean(value: str, *, limit: int) -> str:
        compact = " ".join(value.split())
        compact = RuleBasedSemanticExtractor._SECRET.sub("[REDACTED]", compact)
        return compact if len(compact) <= limit else compact[: limit - 3] + "..."

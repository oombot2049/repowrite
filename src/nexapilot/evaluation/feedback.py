from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from nexapilot.model import MessageWithParts, TextPart

if TYPE_CHECKING:
    from nexapilot.store.sqlite import SQLiteStore


FeedbackRating = Literal["positive", "negative"]
FeedbackErrorType = Literal[
    "incorrect",
    "incomplete",
    "instruction_not_followed",
    "tool_failure",
    "unsafe",
    "outdated",
    "other",
]
EvalCandidateStatus = Literal["pending", "accepted", "rejected"]

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}

_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(?:sk|sk-proj|sk-or-v1|ghp|github_pat)_"
            r"[A-Za-z0-9_-]{12,}\b|\bsk-[A-Za-z0-9_-]{12,}\b"
        ),
        "[REDACTED_TOKEN]",
    ),
    (
        re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
        r"\1[REDACTED_TOKEN]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    (
        re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+"),
        r"C:\\Users\\[REDACTED]",
    ),
    (
        re.compile(r"(?i)(?<![A-Za-z0-9_])/(?:home|Users)/[^/\s]+"),
        "/home/[REDACTED]",
    ),
)


class FeedbackSubmission(BaseModel):
    rating: FeedbackRating
    error_types: list[FeedbackErrorType] = Field(default_factory=list, max_length=7)
    comment: str = Field(default="", max_length=4_000)

    @field_validator("error_types")
    @classmethod
    def unique_error_types(
        cls, values: list[FeedbackErrorType]
    ) -> list[FeedbackErrorType]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_rating_details(self) -> FeedbackSubmission:
        if self.rating == "negative" and not self.error_types:
            raise ValueError("negative feedback requires at least one error type")
        if self.rating == "positive" and self.error_types:
            raise ValueError("positive feedback cannot include error types")
        return self


class RunFeedback(BaseModel):
    id: str
    run_id: str
    session_id: str
    rating: FeedbackRating
    error_types: list[FeedbackErrorType] = Field(default_factory=list)
    comment_redacted: str = ""
    redaction_count: int = 0
    created_at: int


class EvalCandidate(BaseModel):
    id: str
    feedback_id: str
    run_id: str
    session_id: str
    status: EvalCandidateStatus = "pending"
    prompt_redacted: str
    response_redacted: str
    error_types: list[FeedbackErrorType] = Field(default_factory=list)
    feedback_redacted: str = ""
    run_status: str
    source_message_ids: list[str] = Field(default_factory=list)
    redaction_count: int = 0
    reviewer_note: str = ""
    reviewed_at: int | None = None
    created_at: int
    updated_at: int


class EvalCandidateReview(BaseModel):
    decision: Literal["accept", "reject"]
    note: str = Field(default="", max_length=2_000)


def redact_feedback_text(text: str, *, limit: int = 8_000) -> tuple[str, int]:
    """Return a bounded, control-character-safe value without common secrets or PII."""
    cleaned = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    redactions = 0
    for pattern, replacement in _REDACTION_RULES:
        cleaned, count = pattern.subn(replacement, cleaned)
        redactions += count
    return cleaned[:limit].strip(), redactions


def _message_text(message: MessageWithParts | None) -> str:
    if message is None:
        return ""
    return "".join(part.text for part in message.parts if isinstance(part, TextPart)).strip()


class FeedbackService:
    """Turns immutable Run feedback into review-gated bad-case candidates."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def submit(
        self, run_id: str, submission: FeedbackSubmission
    ) -> tuple[RunFeedback, EvalCandidate | None, bool]:
        run = await self._store.get_run(run_id)
        if run.status not in TERMINAL_RUN_STATUSES:
            raise ValueError("feedback is only accepted for terminal runs")

        messages = await self._store.list_messages(run.session_id)
        trigger = next(
            (item for item in messages if item.info.id == run.trigger_message_id), None
        )
        assistant = next(
            (item for item in messages if item.info.id == run.assistant_message_id), None
        )

        comment, comment_redactions = redact_feedback_text(submission.comment, limit=4_000)
        now = int(time.time() * 1000)
        feedback = RunFeedback(
            id=str(uuid4()),
            run_id=run.id,
            session_id=run.session_id,
            rating=submission.rating,
            error_types=submission.error_types,
            comment_redacted=comment,
            redaction_count=comment_redactions,
            created_at=now,
        )

        candidate: EvalCandidate | None = None
        if submission.rating == "negative":
            prompt, prompt_redactions = redact_feedback_text(_message_text(trigger))
            response, response_redactions = redact_feedback_text(_message_text(assistant))
            source_ids = [
                message_id
                for message_id in (run.trigger_message_id, run.assistant_message_id)
                if message_id
            ]
            candidate = EvalCandidate(
                id=str(uuid4()),
                feedback_id=feedback.id,
                run_id=run.id,
                session_id=run.session_id,
                prompt_redacted=prompt,
                response_redacted=response,
                error_types=submission.error_types,
                feedback_redacted=comment,
                run_status=run.status,
                source_message_ids=source_ids,
                redaction_count=comment_redactions
                + prompt_redactions
                + response_redactions,
                created_at=now,
                updated_at=now,
            )

        return await self._store.create_run_feedback(feedback, candidate)

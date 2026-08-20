from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class SideEffect(StrEnum):
    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"


class Idempotency(StrEnum):
    SAFE = "safe"
    REQUIRES_KEY = "requires_key"
    UNSAFE = "unsafe"


class RetryPolicy(StrEnum):
    NEVER = "never"
    TRANSIENT_ONLY = "transient_only"


class Compensation(StrEnum):
    NONE = "none"
    MANUAL = "manual"
    TOOL = "tool"


class ApprovalScope(StrEnum):
    ONCE = "once"
    ARGUMENTS = "arguments"
    SESSION = "session"


@dataclass(frozen=True)
class ToolContract:
    side_effect: SideEffect
    idempotency: Idempotency
    retry: RetryPolicy
    compensation: Compensation
    approval_scope: ApprovalScope
    compensation_tool: str | None = None

    def __post_init__(self) -> None:
        if self.compensation is Compensation.TOOL and not self.compensation_tool:
            raise ValueError("compensation_tool is required when compensation=tool")
        if self.compensation is not Compensation.TOOL and self.compensation_tool:
            raise ValueError("compensation_tool is only valid when compensation=tool")
        if self.side_effect is SideEffect.NONE and self.idempotency is not Idempotency.SAFE:
            raise ValueError("side-effect-free tools must be idempotent")

    def to_metadata(self) -> dict[str, str | None]:
        return {
            "side_effect": self.side_effect.value,
            "idempotency": self.idempotency.value,
            "retry": self.retry.value,
            "compensation": self.compensation.value,
            "approval_scope": self.approval_scope.value,
            "compensation_tool": self.compensation_tool,
        }

    def recovery_action(self, *, idempotency_key_present: bool = False) -> str:
        if self.side_effect is SideEffect.NONE or self.idempotency is Idempotency.SAFE:
            return "safe_to_retry"
        if self.idempotency is Idempotency.REQUIRES_KEY and idempotency_key_present:
            return "retry_with_same_key"
        return "manual_review"


CONSERVATIVE_TOOL_CONTRACT = ToolContract(
    side_effect=SideEffect.EXTERNAL_WRITE,
    idempotency=Idempotency.UNSAFE,
    retry=RetryPolicy.NEVER,
    compensation=Compensation.MANUAL,
    approval_scope=ApprovalScope.ONCE,
)


def get_tool_contract(tool: object) -> ToolContract:
    contract = getattr(tool, "contract", None)
    return contract if isinstance(contract, ToolContract) else CONSERVATIVE_TOOL_CONTRACT


@dataclass(frozen=True)
class ToolResult:
    title: str
    output: str
    metadata: dict[str, Any]


class ToolContext(Protocol):
    session_id: str
    message_id: str
    agent: str
    source: str
    tool_part_id: str
    trace_id: str | None
    parent_observation_id: str | None
    root_session_id: str | None
    parent_session_id: str | None
    parallel_group_id: str | None
    parallel_index: int | None
    parallel_size: int | None

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict[str, Any]) -> None: ...
    async def tool_stream_update(self, output: str) -> None: ...


class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> dict[str, Any]: ...
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

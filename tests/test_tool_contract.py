from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexapilot.loop.session_loop import ToolCtx
from nexapilot.tools.base import (
    ApprovalScope,
    Compensation,
    Idempotency,
    RetryPolicy,
    SideEffect,
    ToolContract,
    get_tool_contract,
)
from nexapilot.tools.files import ReadTool, WriteTool


def test_contract_validates_compensation_and_side_effect_invariants() -> None:
    with pytest.raises(ValueError, match="compensation_tool is required"):
        ToolContract(
            SideEffect.EXTERNAL_WRITE,
            Idempotency.UNSAFE,
            RetryPolicy.NEVER,
            Compensation.TOOL,
            ApprovalScope.ONCE,
        )

    with pytest.raises(ValueError, match="side-effect-free"):
        ToolContract(
            SideEffect.NONE,
            Idempotency.UNSAFE,
            RetryPolicy.NEVER,
            Compensation.NONE,
            ApprovalScope.ONCE,
        )


def test_builtin_file_contracts_are_explicit() -> None:
    assert ReadTool.contract.side_effect is SideEffect.NONE
    assert ReadTool.contract.recovery_action() == "safe_to_retry"
    assert WriteTool.contract.side_effect is SideEffect.LOCAL_WRITE
    assert WriteTool.contract.idempotency is Idempotency.SAFE


def test_undeclared_tool_uses_conservative_contract() -> None:
    class LegacyTool:
        pass

    contract = get_tool_contract(LegacyTool())
    assert contract.side_effect is SideEffect.EXTERNAL_WRITE
    assert contract.idempotency is Idempotency.UNSAFE
    assert contract.recovery_action() == "manual_review"


def test_keyed_contract_requires_the_same_key_for_recovery() -> None:
    contract = ToolContract(
        SideEffect.EXTERNAL_WRITE,
        Idempotency.REQUIRES_KEY,
        RetryPolicy.TRANSIENT_ONLY,
        Compensation.MANUAL,
        ApprovalScope.ARGUMENTS,
    )
    assert contract.recovery_action() == "manual_review"
    assert contract.recovery_action(idempotency_key_present=True) == "retry_with_same_key"


@dataclass
class _PermissionRecorder:
    calls: list[dict]

    async def ask(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected_always"),
    [
        (ApprovalScope.ONCE, []),
        (ApprovalScope.ARGUMENTS, ["target"]),
        (ApprovalScope.SESSION, ["*"]),
    ],
)
async def test_tool_context_enforces_approval_scope(
    scope: ApprovalScope, expected_always: list[str]
) -> None:
    recorder = _PermissionRecorder([])
    ctx = ToolCtx(
        session_id="session",
        message_id="message",
        agent="primary",
        source="api",
        bus=object(),
        store=object(),
        perm=recorder,
        ruleset=[],
        tool_part_id="call",
        approval_scope=scope,
    )
    await ctx.ask(
        permission="write",
        patterns=["target"],
        always=["target"],
        metadata={},
    )
    assert recorder.calls[0]["always"] == expected_always
    assert recorder.calls[0]["metadata"]["approval_scope"] == scope.value

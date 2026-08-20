from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from nexapilot.model import PermissionRule

CheckType = Literal[
    "run_status",
    "assistant_contains",
    "assistant_not_contains",
    "file_exists",
    "file_not_exists",
    "file_contains",
    "file_not_contains",
    "command",
    "changed_files",
    "tool_called",
    "tool_not_called",
    "no_tool_errors",
    "artifact_exists",
]
CheckCategory = Literal["correctness", "quality", "safety", "efficiency"]


class EvalCheck(BaseModel):
    type: CheckType
    name: str = ""
    category: CheckCategory = "correctness"
    weight: float = Field(default=1.0, ge=0)
    hard_gate: bool = False
    path: str | None = None
    contains: str | None = None
    pattern: str | None = None
    command: list[str] | None = None
    expected_exit_code: int = 0
    timeout_ms: int = Field(default=30_000, ge=1, le=600_000)
    tool: str | None = None
    statuses: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    required_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    artifact_name: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> EvalCheck:
        if (
            self.type
            in {"file_exists", "file_not_exists", "file_contains", "file_not_contains"}
            and not self.path
        ):
            raise ValueError(f"{self.type} requires path")
        if (
            self.type
            in {
                "assistant_contains",
                "assistant_not_contains",
                "file_contains",
                "file_not_contains",
            }
            and self.contains is None
            and self.pattern is None
        ):
            raise ValueError(f"{self.type} requires contains or pattern")
        if self.type == "command" and not self.command:
            raise ValueError("command requires a non-empty argv list")
        if self.type in {"tool_called", "tool_not_called"} and not self.tool:
            raise ValueError(f"{self.type} requires tool")
        if self.type == "artifact_exists" and not self.artifact_name:
            raise ValueError("artifact_exists requires artifact_name")
        return self


class EvalBudget(BaseModel):
    max_duration_ms: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost_microusd: int | None = Field(default=None, ge=0)


class EvalCase(BaseModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = ""
    prompt: str = Field(min_length=1)
    fixture: str | None = None
    permission_mode: Literal["allow", "deny", "default"] = "allow"
    permission_rules: list[PermissionRule] | None = None
    checks: list[EvalCheck] = Field(min_length=1)
    budget: EvalBudget = Field(default_factory=EvalBudget)
    min_score: float = Field(default=1.0, ge=0, le=1)
    timeout_seconds: float = Field(default=600.0, gt=0, le=3600)


class EvalDataset(BaseModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1)
    description: str = ""
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> EvalDataset:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


class EvalObservation(BaseModel):
    session_id: str
    run_id: str
    workspace: str
    run: dict[str, Any]
    messages: list[dict[str, Any]] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    workspace_view: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)
    llm_totals: dict[str, int] = Field(default_factory=dict)
    assistant_text: str = ""
    duration_ms: int = 0


class CheckResult(BaseModel):
    type: CheckType | Literal["budget"]
    name: str
    category: CheckCategory
    passed: bool
    hard_gate: bool
    weight: float
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class CaseResult(BaseModel):
    id: str
    name: str
    passed: bool
    score: float
    min_score: float
    checks: list[CheckResult]
    observation: EvalObservation | None = None
    error: str | None = None
    workspace_preserved: bool = False


class EvalSummary(BaseModel):
    cases: int
    passed: int
    failed: int
    task_success_rate: float
    mean_score: float
    hard_gate_failures: int
    safety_violations: int
    mean_duration_ms: float
    total_model_calls: int
    total_tool_calls: int
    total_tokens: int
    total_cost_microusd: int


class EvalReport(BaseModel):
    schema_version: Literal[1] = 1
    dataset_name: str
    dataset_path: str
    started_at: str
    completed_at: str
    summary: EvalSummary
    cases: list[CaseResult]
    baseline: dict[str, Any] | None = None
    regressions: list[str] = Field(default_factory=list)

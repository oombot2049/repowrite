from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

Role = Literal["user", "assistant", "tool"]
SessionKind = Literal["primary", "subagent"]
RunStatus = Literal[
    "queued",
    "acquiring",
    "running",
    "waiting_approval",
    "waiting_retry",
    "recovery_pending",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
OutboxEventStatus = Literal["pending", "processing", "processed", "dead_letter"]

# Todo types
TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TodoPriority = Literal["high", "medium", "low"]
GoalStatus = Literal["active", "paused", "completed", "failed", "cancelled"]
PlanStatus = Literal[
    "draft", "active", "paused", "completed", "failed", "superseded", "cancelled"
]
PlanTaskStatus = Literal[
    "pending",
    "ready",
    "running",
    "waiting_retry",
    "paused",
    "completed",
    "failed",
    "cancelled",
]
TaskExecutionMode = Literal["agent", "human"]
CronScheduleKind = Literal["at", "every", "cron"]
CronJobStatus = Literal["running", "ok", "error", "skipped"]


class TodoItem(BaseModel):
    """A single todo item in the session's task list."""
    id: str  # Unique identifier (UUID) for reconciliation
    content: str  # Task description in imperative form, e.g., "Run tests"
    status: TodoStatus = "pending"
    priority: TodoPriority = "medium"
    activeForm: str  # Present continuous form, e.g., "Running tests..."


class Goal(BaseModel):
    id: str
    session_id: str
    title: str
    description: str = ""
    status: GoalStatus = "active"
    active_plan_id: str | None = None
    revision: int = 0
    created_at: int
    updated_at: int


class TaskPlan(BaseModel):
    id: str
    goal_id: str
    version: int
    status: PlanStatus = "active"
    rationale: str = ""
    source_run_id: str | None = None
    revision: int = 0
    created_at: int
    updated_at: int


class PlanTask(BaseModel):
    id: str
    plan_id: str
    key: str
    title: str
    description: str = ""
    status: PlanTaskStatus = "pending"
    priority: int = 0
    position: int = 0
    execution_mode: TaskExecutionMode = "agent"
    assignee: str | None = None
    attempt: int = 0
    max_attempts: int = 1
    next_attempt_at: int | None = None
    owner_id: str | None = None
    lease_until: int | None = None
    last_run_id: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    revision: int = 0
    created_at: int
    updated_at: int


class TaskRuntimeEvent(BaseModel):
    id: str
    sequence: int
    session_id: str
    goal_id: str
    plan_id: str | None = None
    task_id: str | None = None
    event_type: str
    actor: str
    reason: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: int


class PlanTaskSpec(BaseModel):
    key: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    description: str = ""
    priority: int = 0
    max_attempts: int = Field(default=1, ge=1, le=20)
    depends_on: list[str] = Field(default_factory=list)


class CreateGoalRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = ""
    rationale: str = ""
    source_run_id: str | None = None
    actor: str = "user"
    tasks: list[PlanTaskSpec] = Field(..., min_length=1, max_length=100)


class CreatePlanRequest(BaseModel):
    rationale: str = ""
    source_run_id: str | None = None
    actor: str = "user"
    expected_goal_revision: int
    tasks: list[PlanTaskSpec] = Field(..., min_length=1, max_length=100)


class TaskTransitionRequest(BaseModel):
    status: PlanTaskStatus
    expected_revision: int
    actor: str = "user"
    reason: str | None = None
    run_id: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    retry_delay_ms: int = Field(default=0, ge=0, le=86_400_000)


class RevisionActionRequest(BaseModel):
    expected_revision: int
    actor: str = "user"
    reason: str | None = None


class ReorderPlanRequest(BaseModel):
    task_ids: list[str] = Field(..., min_length=1)
    expected_revision: int
    actor: str = "user"
    reason: str | None = None


class TaskTakeoverRequest(BaseModel):
    expected_revision: int
    actor: str = "user"
    assignee: str = Field(..., min_length=1, max_length=200)
    reason: str | None = None


class TaskReleaseRequest(BaseModel):
    expected_revision: int
    actor: str = "user"
    reason: str | None = None


class CronSchedule(BaseModel):
    kind: CronScheduleKind
    at_ms: int | None = None
    every_ms: int | None = None
    expr: str | None = None
    tz: str | None = None


class CronPayload(BaseModel):
    kind: Literal["agent_turn"] = "agent_turn"
    message: str


class CronJobState(BaseModel):
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: CronJobStatus | None = None
    last_error: str | None = None
    last_assistant_message_id: str | None = None
    last_trace_id: str | None = None


class CronJob(BaseModel):
    id: str
    name: str
    session_id: str
    enabled: bool = True
    schedule: CronSchedule
    payload: CronPayload
    state: CronJobState = Field(default_factory=CronJobState)
    created_at_ms: int
    updated_at_ms: int
    delete_after_run: bool = False


class CronJobRun(BaseModel):
    id: int
    job_id: str
    session_id: str
    started_at_ms: int
    finished_at_ms: int | None = None
    status: CronJobStatus
    error: str | None = None
    assistant_message_id: str | None = None
    trace_id: str | None = None


class ModelRef(BaseModel):
    provider: str
    id: str


class PermissionRule(BaseModel):
    permission: str
    pattern: str
    action: Literal["allow", "deny", "ask"]


class DaytonaRuntimeConfig(BaseModel):
    sandbox_id: str | None = None
    sandbox_name: str | None = None


class SessionRuntime(BaseModel):
    backend: Literal["local", "daytona"] = "local"
    daytona: DaytonaRuntimeConfig | None = None


class Project(BaseModel):
    id: str
    name: str
    root_path: str
    created_at: int
    updated_at: int
    last_opened_at: int


class Session(BaseModel):
    id: str
    title: str
    worktree: str
    cwd: str
    created_at: int
    updated_at: int
    permission_rules: list[PermissionRule]
    runtime: SessionRuntime = Field(default_factory=SessionRuntime)
    kind: SessionKind = "primary"
    agent_name: str = "primary"
    root_session_id: str | None = None
    parent_session_id: str | None = None
    parent_tool_call_id: str | None = None
    project_worktree: str | None = None
    project_id: str | None = None

    @property
    def memory_worktree(self) -> str:
        return self.project_worktree or self.worktree


class AgentWorkspaceReleaseRequest(BaseModel):
    force: bool = False


class Run(BaseModel):
    id: str
    session_id: str
    sequence: int
    trigger_message_id: str | None = None
    assistant_message_id: str | None = None
    status: RunStatus = "queued"
    source: str
    agent_name: str
    model: ModelRef
    started_at: int | None = None
    completed_at: int | None = None
    finish_reason: str | None = None
    error: dict[str, Any] | None = None
    model_rounds: int = 0
    tool_call_count: int = 0
    input_sequence: int | None = None
    output_sequence: int | None = None
    revision: int = 0
    state_reason: str | None = None
    owner_id: str | None = None
    lease_until: int | None = None
    heartbeat_at: int | None = None
    attempt: int = 0
    max_attempts: int = 1
    next_attempt_at: int | None = None
    checkpoint_seq: int = 0
    checkpoint: dict[str, Any] | None = None
    cancel_requested_at: int | None = None
    error_code: str | None = None
    error_summary: str | None = None
    created_at: int
    updated_at: int


class RunStep(BaseModel):
    id: str
    run_id: str
    sequence: int
    kind: str
    status: str
    input_ref: str | None = None
    output_ref: str | None = None
    operation_id: str | None = None
    started_at: int
    finished_at: int | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    id: str
    session_id: str
    run_id: str | None = None
    message_id: str | None = None
    tool_call_id: str | None = None
    kind: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    storage_path: str
    preview: str = ""
    created_at: int


class OutboxEvent(BaseModel):
    id: str
    idempotency_key: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    session_id: str | None = None
    run_id: str | None = None
    sequence_from: int | None = None
    sequence_to: int | None = None
    payload: dict[str, Any]
    status: OutboxEventStatus = "pending"
    attempts: int = 0
    next_retry_at: int | None = None
    claimed_at: int | None = None
    claimed_by: str | None = None
    last_error: str | None = None
    created_at: int
    processed_at: int | None = None


class MemoryCheckpoint(BaseModel):
    processor_name: str
    session_id: str
    last_message_sequence: int = 0
    updated_at: int


class Episode(BaseModel):
    id: str
    workspace: str
    source_session_id: str
    source_run_id: str
    source_kind: Literal["primary", "subagent"] = "primary"
    source_agent: str = "primary"
    sequence_from: int
    sequence_to: int
    goal: str
    actions: list[str] = Field(default_factory=list)
    outcome: str
    errors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    extractor_version: str
    created_at: int
    updated_at: int


class EpisodeHit(BaseModel):
    episode: Episode
    score: float


class SemanticMemory(BaseModel):
    id: str
    namespace: str
    workspace: str
    memory_type: Literal["profile", "fact", "preference", "decision", "goal", "constraint", "lesson"]
    subject: str
    predicate: str
    value: str
    status: Literal["candidate", "active", "superseded", "expired", "deleted", "rejected"] = "candidate"
    confidence: float = 0.5
    importance: float = 0.5
    valid_from: int | None = None
    valid_to: int | None = None
    source_session_id: str
    source_run_id: str | None = None
    source_kind: Literal["primary", "subagent"] = "primary"
    source_agent: str = "primary"
    source_message_ids: list[str] = Field(default_factory=list)
    content_hash: str
    version: int = 1
    extractor_version: str
    created_at: int
    updated_at: int


class SemanticMemoryHit(BaseModel):
    memory: SemanticMemory
    score: float


class CoreMemoryBlock(BaseModel):
    id: str
    workspace: str
    namespace: str
    block_type: Literal[
        "project_profile",
        "user_preferences",
        "active_constraints",
        "active_goals",
        "critical_decisions",
    ]
    content: str
    source_memory_ids: list[str] = Field(default_factory=list)
    priority: int
    token_count: int
    content_hash: str
    version: int = 1
    created_at: int
    updated_at: int


class Message(BaseModel):
    id: str
    session_id: str
    run_id: str | None = None
    sequence: int | None = None
    role: Role
    parent_id: Optional[str] = None
    agent: str
    model: ModelRef
    created_at: int
    completed_at: Optional[int] = None
    finish: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    # Token tracking (for assistant messages)
    tokens: Optional[dict[str, int]] = None  # {"input": n, "output": n, "reasoning": n}
    cost: Optional[float] = None  # USD


class TextPart(BaseModel):
    id: str  # Part ID (ULID or UUID)
    message_id: str
    session_id: str
    type: Literal["text"] = "text"
    text: str
    synthetic: bool = False
    time: Optional[dict[str, int]] = None  # {"start": ts, "end": ts}


class ToolStatePending(BaseModel):
    status: Literal["pending"] = "pending"
    input: dict[str, Any] = Field(default_factory=dict)
    raw: str = ""


class ToolStateRunning(BaseModel):
    status: Literal["running"] = "running"
    input: dict[str, Any]
    title: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    time: dict[str, int]


class ToolStateCompleted(BaseModel):
    status: Literal["completed"] = "completed"
    input: dict[str, Any]
    title: str
    output: str
    metadata: dict[str, Any]
    time: dict[str, int]


class ToolStateError(BaseModel):
    status: Literal["error"] = "error"
    input: dict[str, Any]
    error: str
    metadata: Optional[dict[str, Any]] = None
    time: dict[str, int]


ToolState = Union[ToolStatePending, ToolStateRunning, ToolStateCompleted, ToolStateError]


class ToolPart(BaseModel):
    id: str  # Part ID
    message_id: str
    session_id: str
    type: Literal["tool"] = "tool"
    call_id: str
    tool: str
    state: ToolState


class ReasoningPart(BaseModel):
    id: str
    message_id: str
    session_id: str
    type: Literal["reasoning"] = "reasoning"
    text: str
    time: dict[str, int]  # {"start": ts, "end": ts}


class ProviderStatePart(BaseModel):
    id: str
    message_id: str
    session_id: str
    type: Literal["provider_state"] = "provider_state"
    provider: str
    data: dict[str, Any]


Part = Union[TextPart, ToolPart, ReasoningPart, ProviderStatePart]


class MessageWithParts(BaseModel):
    info: Message
    parts: list[Part]


class PermissionRequest(BaseModel):
    id: str
    session_id: str
    permission: str
    patterns: list[str]
    metadata: dict[str, Any]
    always: list[str]
    tool: Optional[dict[str, str]] = None


class PermissionReply(BaseModel):
    reply: Literal["once", "always", "reject"]
    message: Optional[str] = None


# --- API Request Models (for OpenAPI schema) ---

class CreateSessionRequest(BaseModel):
    worktree: str = Field(default="", description="Legacy absolute worktree path")
    project_id: str | None = Field(
        default=None, description="Owning project; its root becomes the worktree"
    )
    title: str = Field(default="New session", description="Session title")
    cwd: str = Field(default="", description="Current working directory (defaults to worktree)")
    permission_rules: Optional[list[PermissionRule]] = Field(default=None, description="Permission rules (defaults to global config)")
    runtime: SessionRuntime | None = Field(default=None, description="Session runtime backend (local or daytona)")


class AddMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User message text")


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1, description="New session title")


class CreateProjectRequest(BaseModel):
    name: str = Field(default="", description="Project display name; defaults to folder name")
    root_path: str = Field(..., min_length=1, description="Absolute local project root")


class RenameProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, description="New project display name")


class CreateCronJobRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Cron job name")
    session_id: str = Field(..., min_length=1, description="Target session ID")
    schedule: CronSchedule
    message: str = Field(..., min_length=1, description="Message to inject into the session when triggered")
    enabled: bool = True
    delete_after_run: bool = False


class CronJobEnabledRequest(BaseModel):
    enabled: bool = Field(..., description="Enable or disable the job")


class CronJobRunRequest(BaseModel):
    force: bool = Field(False, description="Allow manual run for disabled jobs")

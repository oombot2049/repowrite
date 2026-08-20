from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nexapilot.model import ModelRef, PermissionRule

AgentMode = Literal["primary", "subagent"]
WorkspaceMode = Literal["shared", "git_worktree"]
WorkspaceCleanup = Literal["manual"]


@dataclass(frozen=True)
class AgentLimits:
    max_turns: int | None = None
    max_tool_calls: int | None = None
    max_wall_time_ms: int | None = None
    max_concurrency: int | None = None
    max_input_tokens: int | None = None


@dataclass(frozen=True)
class AgentWorkspacePolicy:
    mode: WorkspaceMode = "shared"
    cleanup: WorkspaceCleanup = "manual"


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    mode: AgentMode
    description: str
    capabilities: frozenset[str] = frozenset()
    prompt: str | None = None
    prompt_template_path: str | None = None
    tool_allowlist: frozenset[str] | None = None
    permission_profile: tuple[PermissionRule, ...] = ()
    limits: AgentLimits = field(default_factory=AgentLimits)
    workspace: AgentWorkspacePolicy = field(default_factory=AgentWorkspacePolicy)
    model_override: ModelRef | None = None
    source: str = "builtin"

    def load_prompt(self) -> str:
        if self.prompt is not None:
            return self.prompt
        if not self.prompt_template_path:
            return ""
        path = Path(self.prompt_template_path)
        return path.read_text(encoding="utf-8") if path.is_file() else ""


@dataclass(frozen=True)
class RunRequest:
    session_id: str
    agent_name: str
    source: str = "api"
    root_session_id: str | None = None
    parent_session_id: str | None = None
    parent_tool_call_id: str | None = None
    trace_id: str | None = None
    parent_observation_id: str | None = None
    limits: AgentLimits = field(default_factory=AgentLimits)


@dataclass(frozen=True)
class RunResult:
    assistant_message_id: str
    trace_id: str | None
    finish: str | None = None

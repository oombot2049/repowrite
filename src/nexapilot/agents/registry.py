from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from nexapilot.agents.types import (
    AgentDefinition,
    AgentLimits,
    AgentMode,
    AgentWorkspacePolicy,
    WorkspaceMode,
)
from nexapilot.model import ModelRef, PermissionRule

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "agents"
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VALID_MODES = {"primary", "subagent"}


class AgentRegistry:
    def __init__(self, agents: tuple[AgentDefinition, ...]) -> None:
        if not agents:
            raise ValueError("agent registry must not be empty")
        by_name: dict[str, AgentDefinition] = {}
        for agent in agents:
            if not _AGENT_NAME_RE.fullmatch(agent.name):
                raise ValueError(f"invalid agent name: {agent.name}")
            if agent.name in by_name:
                raise ValueError(f"duplicate agent name: {agent.name}")
            if agent.mode not in _VALID_MODES:
                raise ValueError(f"invalid agent mode for {agent.name}: {agent.mode}")
            if not agent.description.strip():
                raise ValueError(f"agent description is required: {agent.name}")
            by_name[agent.name] = agent
        primary = [agent for agent in agents if agent.mode == "primary"]
        if len(primary) != 1:
            raise ValueError("agent registry must contain exactly one primary agent")
        self._agents = by_name

    def get(self, name: str) -> AgentDefinition:
        agent = self._agents.get(name)
        if not agent:
            raise KeyError(f"unknown agent: {name}")
        return agent

    def list(self, *, mode: str | None = None) -> list[AgentDefinition]:
        agents = list(self._agents.values())
        if mode is None:
            return agents
        return [agent for agent in agents if agent.mode == mode]

    def merged(self, overrides: tuple[AgentDefinition, ...]) -> AgentRegistry:
        merged = dict(self._agents)
        for agent in overrides:
            merged[agent.name] = agent
        return AgentRegistry(tuple(merged.values()))


def _builtin_registry() -> AgentRegistry:
    return AgentRegistry(
        (
            AgentDefinition(
                name="primary",
                mode="primary",
                description="Default primary agent.",
                capabilities=frozenset({"orchestration", "coding"}),
            ),
            AgentDefinition(
                name="explore",
                mode="subagent",
                description="Read-only code exploration agent.",
                capabilities=frozenset({"code_search", "research"}),
                prompt_template_path=str(_PROMPTS_DIR / "explore.txt"),
                tool_allowlist=frozenset(
                    {
                        "read",
                        "glob",
                        "grep",
                        "memory_search",
                        "memory_get",
                        "websearch",
                        "webfetch",
                    }
                ),
                permission_profile=(
                    PermissionRule(
                        permission="external_directory", pattern="*", action="ask"
                    ),
                    PermissionRule(permission="task", pattern="*", action="deny"),
                    PermissionRule(permission="webfetch", pattern="*", action="allow"),
                    PermissionRule(permission="websearch", pattern="*", action="allow"),
                    PermissionRule(
                        permission="memory_get", pattern="*", action="allow"
                    ),
                    PermissionRule(
                        permission="memory_search", pattern="*", action="allow"
                    ),
                    PermissionRule(permission="grep", pattern="*", action="allow"),
                    PermissionRule(permission="glob", pattern="*", action="allow"),
                    PermissionRule(permission="read", pattern="*", action="allow"),
                    PermissionRule(permission="*", pattern="*", action="deny"),
                ),
                limits=AgentLimits(
                    max_turns=8,
                    max_tool_calls=24,
                    max_wall_time_ms=300_000,
                    max_concurrency=2,
                    max_input_tokens=16_000,
                ),
            ),
        )
    )


def _positive_limit(raw: dict[str, Any], name: str, *, maximum: int) -> int | None:
    value = raw.get(name)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        raise ValueError(
            f"agent limit {name} must be an integer between 1 and {maximum}"
        )
    return value


def _resolve_prompt_file(root: Path, value: str) -> str:
    candidate = Path(value)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    )
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"agent prompt_file escapes project root: {value}") from exc
    if not resolved.is_file():
        raise ValueError(f"agent prompt_file not found: {value}")
    return str(resolved)


def _parse_agent(raw: Any, *, root: Path, source: str) -> AgentDefinition:
    if not isinstance(raw, dict):
        raise TypeError("each agent definition must be an object")
    name = str(raw.get("name") or "").strip()
    mode = str(raw.get("mode") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid agent name: {name or '<empty>'}")
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid agent mode for {name}: {mode or '<empty>'}")
    if not description:
        raise ValueError(f"agent description is required: {name}")

    prompt = raw.get("prompt")
    prompt_file = raw.get("prompt_file")
    if prompt is not None and prompt_file is not None:
        raise ValueError(f"agent {name} cannot define both prompt and prompt_file")
    prompt_text = str(prompt) if prompt is not None else None
    prompt_path = (
        _resolve_prompt_file(root, str(prompt_file))
        if prompt_file is not None
        else None
    )

    tools = raw.get("tools")
    permissions = raw.get("permissions")
    if mode == "subagent" and not isinstance(tools, list):
        raise ValueError(f"subagent {name} must declare a tools list")
    if mode == "subagent" and not isinstance(permissions, list):
        raise ValueError(f"subagent {name} must declare a permissions list")
    tool_allowlist = (
        frozenset(str(tool).strip() for tool in tools if str(tool).strip())
        if isinstance(tools, list)
        else None
    )
    permission_profile = tuple(
        PermissionRule.model_validate(item) for item in (permissions or [])
    )

    raw_limits = raw.get("limits") or {}
    if not isinstance(raw_limits, dict):
        raise TypeError(f"agent limits must be an object: {name}")
    limits = AgentLimits(
        max_turns=_positive_limit(raw_limits, "max_turns", maximum=100),
        max_tool_calls=_positive_limit(raw_limits, "max_tool_calls", maximum=1_000),
        max_wall_time_ms=_positive_limit(
            raw_limits, "max_wall_time_ms", maximum=86_400_000
        ),
        max_concurrency=_positive_limit(raw_limits, "max_concurrency", maximum=64),
        max_input_tokens=_positive_limit(
            raw_limits, "max_input_tokens", maximum=1_000_000
        ),
    )

    raw_model = raw.get("model")
    model = None
    if raw_model is not None:
        if not isinstance(raw_model, dict):
            raise ValueError(f"agent model must be an object: {name}")
        model = ModelRef.model_validate(raw_model)

    capabilities = raw.get("capabilities") or []
    if not isinstance(capabilities, list):
        raise TypeError(f"agent capabilities must be a list: {name}")
    raw_workspace = raw.get("workspace") or {}
    if not isinstance(raw_workspace, dict):
        raise TypeError(f"agent workspace must be an object: {name}")
    workspace_mode = str(raw_workspace.get("mode") or "shared").strip()
    cleanup = str(raw_workspace.get("cleanup") or "manual").strip()
    if workspace_mode not in {"shared", "git_worktree"}:
        raise ValueError(f"invalid agent workspace mode for {name}: {workspace_mode}")
    if cleanup != "manual":
        raise ValueError(f"invalid agent workspace cleanup for {name}: {cleanup}")

    return AgentDefinition(
        name=name,
        mode=cast(AgentMode, mode),
        description=description,
        capabilities=frozenset(
            str(item).strip() for item in capabilities if str(item).strip()
        ),
        prompt=prompt_text,
        prompt_template_path=prompt_path,
        tool_allowlist=tool_allowlist,
        permission_profile=permission_profile,
        limits=limits,
        workspace=AgentWorkspacePolicy(
            mode=cast(WorkspaceMode, workspace_mode), cleanup="manual"
        ),
        model_override=model,
        source=source,
    )


def load_agent_registry(worktree: str) -> AgentRegistry:
    root = Path(worktree).resolve()
    path = root / ".nexa" / "agents.yaml"
    registry = _builtin_registry()
    if not path.is_file():
        return registry
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid agent registry YAML: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise TypeError(f"agent registry root must be an object: {path}")
    if document.get("version") != 1:
        raise ValueError(f"unsupported agent registry version in {path}")
    entries = document.get("agents")
    if not isinstance(entries, list):
        raise TypeError(f"agent registry agents must be a list: {path}")
    seen: set[str] = set()
    parsed: list[AgentDefinition] = []
    for raw in entries:
        agent = _parse_agent(raw, root=root, source=str(path))
        if agent.name in seen:
            raise ValueError(f"duplicate agent name in {path}: {agent.name}")
        seen.add(agent.name)
        parsed.append(agent)
    return registry.merged(tuple(parsed))


agent_registry = _builtin_registry()

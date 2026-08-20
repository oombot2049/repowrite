from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Language, Parser
from tree_sitter_bash import language as bash_language

from nexapilot.security.local_guarded import LocalGuardedExecutor, LocalGuardedLimits
from nexapilot.tools.base import (
    ApprovalScope,
    Compensation,
    Idempotency,
    RetryPolicy,
    SideEffect,
    ToolContract,
    ToolResult,
)
from nexapilot.tools.paths import is_within


@dataclass(frozen=True)
class BashCtx:
    worktree: str
    cwd: str
    enabled: bool = True
    require_isolated_shell: bool = False
    default_timeout_ms: int = 120_000
    max_timeout_ms: int = 600_000
    max_output_bytes: int = 2_000_000


def _parser() -> Parser:
    p = Parser()
    cap = bash_language()
    lang = cap if isinstance(cap, Language) else Language(cap)
    if hasattr(p, "set_language"):
        p.set_language(lang)
    else:
        p.language = lang
    return p


def _tokens(src: bytes, node) -> list[str]:
    out: list[str] = []
    for i in range(node.child_count):
        c = node.child(i)
        if not c:
            continue
        if c.type in {"command_name", "word", "string", "raw_string", "concatenation"}:
            out.append(src[c.start_byte : c.end_byte].decode("utf-8", errors="replace"))
    return out


def _commands(src: bytes, tree) -> list[list[str]]:
    root = tree.root_node
    out: list[list[str]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "command":
            t = _tokens(src, n)
            if t:
                out.append(t)
        for i in range(n.child_count - 1, -1, -1):
            c = n.child(i)
            if c:
                stack.append(c)
    return out


class BashTool:
    name = "bash"
    description = "Execute a shell command within the session context."
    contract = ToolContract(
        SideEffect.DESTRUCTIVE,
        Idempotency.UNSAFE,
        RetryPolicy.NEVER,
        Compensation.MANUAL,
        ApprovalScope.ONCE,
    )

    def __init__(self, ctx: BashCtx) -> None:
        self._ctx = ctx
        self._p = _parser()
        self._executor = LocalGuardedExecutor(
            LocalGuardedLimits(
                default_timeout_ms=ctx.default_timeout_ms,
                max_timeout_ms=ctx.max_timeout_ms,
                max_output_bytes=ctx.max_output_bytes,
            )
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["command"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command is required")

        workdir = str(args.get("workdir") or self._ctx.cwd)
        timeout_ms = int(args.get("timeout_ms") or self._ctx.default_timeout_ms)
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be >= 0")

        source = str(getattr(ctx, "source", "api") or "api")
        local_interactive_primary = ctx.agent == "primary" and not (
            source.startswith("channel:")
            or source.startswith("cron")
            or source.startswith("scheduled")
        )
        if not self._ctx.enabled or self._ctx.require_isolated_shell:
            raise RuntimeError(
                "sandbox_capability_unavailable: isolated shell is required"
            )
        if not local_interactive_primary:
            raise RuntimeError(
                "sandbox_capability_unavailable: guarded host shell is only available to an interactive primary agent"
            )

        if not is_within(root=self._ctx.worktree, path=workdir):
            await ctx.ask(
                permission="external_directory",
                patterns=[workdir],
                always=[str(Path(workdir).parent) + "/*"],
                metadata={},
            )

        src = command.encode("utf-8", errors="replace")
        tree = self._p.parse(src)
        cmds = _commands(src, tree)
        patterns = [" ".join(c) for c in cmds if c]
        if patterns:
            await ctx.ask(
                permission="bash",
                patterns=patterns,
                always=patterns,
                metadata={
                    "workdir": str(Path(workdir).resolve()),
                    "capability": "process.exec.shell",
                    "executor": "local_guarded",
                    "isolation": "guarded_host",
                    "risk": "host_command_without_os_filesystem_or_network_isolation",
                },
            )

        path_cmds = {"cd", "rm", "cp", "mv", "mkdir", "touch", "chmod", "chown", "cat"}
        for c in cmds:
            if not c:
                continue
            if c[0] not in path_cmds:
                continue
            for a in c[1:]:
                if a.startswith("-"):
                    continue
                if c[0] == "chmod" and a.startswith("+"):
                    continue
                try:
                    resolved = str((Path(workdir) / a).resolve())
                except Exception:
                    continue
                if is_within(root=self._ctx.worktree, path=resolved):
                    continue
                await ctx.ask(
                    permission="external_directory",
                    patterns=[resolved],
                    always=[str(Path(resolved).parent) + "/*"],
                    metadata={"command": c[0]},
                )

        result = await self._executor.execute(
            command=command,
            cwd=workdir,
            timeout_ms=timeout_ms,
            stream_update=ctx.tool_stream_update,
        )
        return ToolResult(
            title="bash",
            output=result.output,
            metadata={
                "returncode": result.returncode,
                "truncated": result.output_truncated,
                "workdir": str(Path(workdir).resolve()),
                "timeout_ms": result.timeout_ms,
                "executor": "local_guarded",
                "isolation": "guarded_host",
                "security_notice": (
                    "Command ran on the host without OS-level filesystem or network isolation."
                ),
            },
        )

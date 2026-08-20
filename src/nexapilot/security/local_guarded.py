from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class LocalGuardedLimits:
    default_timeout_ms: int = 120_000
    max_timeout_ms: int = 600_000
    max_output_bytes: int = 2_000_000


@dataclass(frozen=True)
class LocalGuardedResult:
    output: str
    returncode: int
    output_truncated: bool
    timeout_ms: int


class LocalGuardedExecutor:
    """Compatibility-first host executor with explicit, enforceable guardrails.

    This is deliberately named guarded rather than sandboxed: it limits inherited
    secrets, wall time, output and process lifetime, but it cannot provide OS-level
    filesystem or network isolation to an approved host command.
    """

    _SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    _CREDENTIAL_CHANNELS = {
        "AWS_PROFILE",
        "AZURE_CONFIG_DIR",
        "GIT_ASKPASS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
    }

    def __init__(self, limits: LocalGuardedLimits) -> None:
        self._limits = limits

    @classmethod
    def sanitized_environment(cls) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in os.environ.items():
            upper = name.upper()
            if upper in cls._CREDENTIAL_CHANNELS or any(
                marker in upper for marker in cls._SECRET_MARKERS
            ):
                continue
            result[name] = value
        result["NEXA_EXECUTOR"] = "local_guarded"
        result["NEXA_ISOLATION"] = "guarded_host"
        return result

    async def execute(
        self,
        *,
        command: str,
        cwd: str,
        timeout_ms: int | None,
        stream_update: Callable[[str], Awaitable[None]],
    ) -> LocalGuardedResult:
        requested_timeout = timeout_ms or self._limits.default_timeout_ms
        effective_timeout = min(
            max(requested_timeout, 1_000), self._limits.max_timeout_ms
        )
        creationflags = 0
        process_kwargs: dict[str, object] = {}
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=self.sanitized_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
            **process_kwargs,
        )
        collected = bytearray()
        truncated = False

        async def pump() -> None:
            nonlocal truncated
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(8_192):
                room = self._limits.max_output_bytes - len(collected)
                if room > 0:
                    collected.extend(chunk[:room])
                    await stream_update(collected.decode("utf-8", errors="replace"))
                if len(chunk) > room:
                    truncated = True

        pump_task = asyncio.create_task(pump())
        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout / 1000)
            await pump_task
        except asyncio.TimeoutError:
            await self._terminate_process_tree(proc)
            if not pump_task.done():
                pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            raise TimeoutError(
                f"local_guarded command timed out after {effective_timeout}ms"
            ) from None
        except asyncio.CancelledError:
            await self._terminate_process_tree(proc)
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            raise

        output = collected.decode("utf-8", errors="replace")
        if truncated:
            output += "\n\n... (output truncated by local_guarded limit)"
        return LocalGuardedResult(
            output=output,
            returncode=int(proc.returncode or 0),
            output_truncated=truncated,
            timeout_ms=effective_timeout,
        )

    async def _terminate_process_tree(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

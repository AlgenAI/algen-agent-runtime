"""Sandbox policy for the processes the Runtime starts: MCP stdio servers and ``core.subprocess``.

A policy sets which environment variables a child inherits (nothing else is passed), its
working directory, POSIX resource limits and an optional wrapper command, such as ``bwrap``,
``firejail``, ``nsjail`` or ``sandbox-exec``, that isolates its filesystem and network. Limits
are applied by a small standard-library launcher (``_sandbox_exec.py``) that sets them,
scrubs the environment and then ``exec``s the wrapper or command, so they hold for
everything the child starts. Children run in a new session, and the Runtime kills the whole
process group on timeout or shutdown.

Resource limits and environment scrubbing are not isolation: without a wrapper a child can
still read every file and reach every host the Runtime's user can. ``security.sandbox.required``
refuses to start any process without a wrapper.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    PolicyDeniedError,
    ToolExecutionError,
)

POSIX = os.name == "posix"
LAUNCHER = Path(__file__).with_name("_sandbox_exec.py")

# What MCP stdio servers have always inherited (the Runtime's list plus the MCP SDK's own).
MCP_DEFAULT_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TEMP", "TMP", "SYSTEMROOT")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceLimits(_Strict):
    """POSIX ``setrlimit`` values for a child; unset keeps the Runtime's own limit.

    ``memory_bytes`` (RLIMIT_AS) is not enforceable on macOS; the launch then fails.
    ``processes`` (RLIMIT_NPROC) counts every process of the user, not only the child's.
    """

    cpu_seconds: int | None = Field(default=None, ge=1)
    memory_bytes: int | None = Field(default=None, ge=16 * 1024 * 1024)
    open_files: int | None = Field(default=None, ge=16)
    processes: int | None = Field(default=None, ge=1)
    file_size_bytes: int | None = Field(default=None, ge=0)

    @property
    def configured(self) -> bool:
        return any(value is not None for value in self.model_dump().values())


class SubprocessSandbox(_Strict):
    env_allowlist: tuple[str, ...] | None = Field(
        default=None,
        description="Names of the Runtime's environment variables a child inherits. Unset: "
        "MCP stdio servers keep PATH, HOME, USER, LOGNAME, SHELL, TERM, TEMP, TMP and "
        "SYSTEMROOT, and core.subprocess inherits nothing. Values a server config sets in "
        "'env' are always passed.",
    )
    working_directory: str | None = Field(
        default=None, description="Working directory for children that do not set their own."
    )
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    wrapper: tuple[str, ...] = Field(
        default=(),
        description="Command prefix that isolates the child, e.g. ['bwrap', '--ro-bind', "
        "'/usr', '/usr', ..., '--']. The command and its arguments follow it.",
    )


class SandboxSettings(SubprocessSandbox):
    required: bool = Field(
        default=False,
        description="Refuse to start any process (MCP stdio server or core.subprocess) without "
        "a wrapper.",
    )
    subprocess_timeout_seconds: float = Field(
        default=60, gt=0, le=3_600, description="core.subprocess wall-clock limit"
    )


@dataclass(frozen=True)
class PreparedCommand:
    """What to execute: the launcher (or wrapper, or command) with its arguments."""

    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str | None


def prepare_command(
    command: str,
    args: Sequence[str],
    *,
    policy: SubprocessSandbox,
    required: bool,
    explicit_env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    default_env: Sequence[str] = (),
    label: str,
) -> PreparedCommand:
    """Apply ``policy`` to one command line. Refuses before anything starts."""
    if required and not policy.wrapper:
        raise PolicyDeniedError(
            f"{label}: security.sandbox.required is set but no sandbox wrapper is configured"
        )
    allowed = policy.env_allowlist if policy.env_allowlist is not None else tuple(default_env)
    allowed_upper = {name.upper() for name in allowed}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or (not POSIX and key.upper() in allowed_upper)
    }
    environment.update(explicit_env or {})
    workdir = cwd or policy.working_directory
    argv = [*policy.wrapper, command, *args]
    if policy.limits.configured and not POSIX:
        raise ConfigurationError(f"{label}: resource limits are supported on POSIX only")
    launcher = POSIX and (
        policy.limits.configured or policy.env_allowlist is not None or bool(policy.wrapper)
    )
    if not launcher:
        return PreparedCommand(argv[0], argv[1:], environment, workdir)
    if not sys.executable:
        raise ConfigurationError(f"{label}: no Python interpreter to run the sandbox launcher")
    spec = {
        "limits": policy.limits.model_dump(exclude_none=True),
        "env": sorted(environment),
    }
    return PreparedCommand(
        sys.executable,
        ["-I", "-S", str(LAUNCHER), json.dumps(spec, separators=(",", ":")), "--", *argv],
        environment,
        workdir,
    )


def kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group (it leads its own session)."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if POSIX:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()


async def run_sandboxed(
    prepared: PreparedCommand, *, timeout_seconds: float, max_output_bytes: int = 1_000_000
) -> tuple[int, bytes, bytes]:
    """Run to completion with no stdin; kill the process group on timeout or cancellation."""
    process = await asyncio.create_subprocess_exec(
        prepared.command,
        *prepared.args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=prepared.env,
        cwd=prepared.cwd,
        start_new_session=POSIX,
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        kill_process_group(process)
        await process.wait()
        raise ToolExecutionError(
            f"the process timed out after {timeout_seconds:g}s and was killed"
        ) from None
    except BaseException:
        kill_process_group(process)
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    # The child is gone; its group may still hold background processes.
    if POSIX:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    assert process.returncode is not None
    return process.returncode, stdout[:max_output_bytes], stderr[:max_output_bytes]

"""CLI-based external agent backends.

One parameterised adapter drives Codex, Claude Code, OpenCode, Pi and Oh My Pi
as subprocesses. Each is detected by checking PATH for its executable and
probing ``--version`` or ``--help``. ``execute()`` runs the process with a
timeout, captures stdout/stderr, and parses JSON when an output schema is
supplied — never raises for a nonzero exit.

Oh My Pi (``omp``) gets the highest priority because it is the preferred Pi
environment per the project specification.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from applyuminati.agents.base import (
    AgentBackend,
    AgentCapability,
    AgentEvent,
    AgentEventKind,
    AgentMetadata,
    AgentResult,
    AgentTask,
    agent_plugin,
)
from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory
from applyuminati.core.registry import HealthReport, HealthState, PluginDescriptor
from applyuminati.core.settings import Settings

__all__ = [
    "CLAUDE_CODE",
    "CODEX",
    "OH_MY_PI",
    "OPENCODE",
    "PI",
    "CliAgentBackend",
    "CliAgentSpec",
]

_CAPABILITIES = frozenset(
    {
        AgentCapability.ONESHOT,
        AgentCapability.FILE_ACCESS,
        AgentCapability.SHELL,
        AgentCapability.CANCELLATION,
    }
)


@dataclass(frozen=True, slots=True)
class CliAgentSpec:
    slug: str
    name: str
    executable: str
    #: How to build argv from the task instruction. ``{prompt}`` is replaced.
    argv_template: tuple[str, ...]
    #: If True, the instruction is piped to stdin instead of passed as argv.
    stdin_prompt: bool = False
    #: How to request JSON output (appended to argv).
    json_flag: str | None = None
    #: How to pass a model override.
    model_flag: str | None = None
    priority: int = 0
    homepage: str | None = None


_SPECS: tuple[CliAgentSpec, ...] = (
    CliAgentSpec(
        slug="oh_my_pi",
        name="Oh My Pi",
        executable="omp",
        argv_template=("run", "{prompt}"),
        stdin_prompt=False,
        json_flag="--json",
        model_flag="--model",
        priority=20,
        homepage="https://github.com/can1357/oh-my-pi",
    ),
    CliAgentSpec(
        slug="codex",
        name="Codex",
        executable="codex",
        argv_template=("exec", "{prompt}"),
        stdin_prompt=True,
        json_flag="--json",
        model_flag="--model",
        priority=15,
        homepage="https://github.com/openai/codex",
    ),
    CliAgentSpec(
        slug="claude_code",
        name="Claude Code",
        executable="claude",
        argv_template=("--print", "{prompt}"),
        stdin_prompt=True,
        json_flag="--output-format",
        model_flag="--model",
        priority=14,
        homepage="https://docs.anthropic.com/en/docs/claude-code",
    ),
    CliAgentSpec(
        slug="opencode",
        name="OpenCode",
        executable="opencode",
        argv_template=("run", "{prompt}"),
        stdin_prompt=False,
        json_flag="--json",
        model_flag="--model",
        priority=10,
    ),
    CliAgentSpec(
        slug="pi",
        name="Pi",
        executable="pi",
        argv_template=("run", "{prompt}"),
        stdin_prompt=False,
        json_flag="--json",
        model_flag="--model",
        priority=8,
    ),
)


class CliAgentBackend(AgentBackend):
    """One external CLI agent runtime, driven as a subprocess."""

    def __init__(self, settings: Settings, spec: CliAgentSpec) -> None:
        self._settings = settings
        self._spec = spec
        # Allow settings to override the executable path.
        override = settings.agents.binaries.get(spec.slug)
        self._executable = override or spec.executable

    @property
    def metadata(self) -> AgentMetadata:
        platforms = (
            frozenset({"darwin", "linux"})
            if self._spec.slug == "oh_my_pi"
            else frozenset({"darwin", "linux", "win32"})
        )
        return AgentMetadata(
            slug=self._spec.slug,
            name=self._spec.name,
            capabilities=_CAPABILITIES,
            executable=self._executable,
            platforms=platforms,
            homepage=self._spec.homepage,
        )

    async def health(self) -> HealthReport:
        path = shutil.which(self._executable)
        if path is None:
            return HealthReport(
                plugin=self._spec.slug,
                state=HealthState.NOT_INSTALLED,
                detail=f"{self._executable!r} not found on PATH",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                version = stdout.decode("utf-8", errors="replace").strip()[:200]
                return HealthReport(
                    plugin=self._spec.slug,
                    state=HealthState.HEALTHY,
                    detail=f"found at {path}",
                    facts={"version": version, "path": path},
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return HealthReport(
                    plugin=self._spec.slug,
                    state=HealthState.HEALTHY,
                    detail=f"found at {path} (version probe timed out)",
                    facts={"path": path},
                )
        except Exception as exc:
            return HealthReport(
                plugin=self._spec.slug,
                state=HealthState.UNAVAILABLE,
                detail=str(exc),
            )

    async def execute(self, task: AgentTask) -> AgentResult:
        argv = [self._executable]
        argv.extend(self._build_argv(task))
        start = utcnow()
        proc: asyncio.subprocess.Process | None = None
        try:
            if self._spec.stdin_prompt:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=task.instruction.encode("utf-8")),
                    timeout=task.timeout_seconds,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=task.timeout_seconds,
                )
        except TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            return AgentResult(
                task_id=task.id,
                backend=self._spec.slug,
                succeeded=False,
                failure_category=FailureCategory.TRANSIENT_NETWORK,
                failure_message=f"timed out after {task.timeout_seconds}s",
                started_at=start,
                finished_at=utcnow(),
                diagnostics="process killed after timeout",
            )
        except FileNotFoundError:
            return AgentResult(
                task_id=task.id,
                backend=self._spec.slug,
                succeeded=False,
                failure_category=FailureCategory.BACKEND_UNAVAILABLE,
                failure_message=f"{self._executable} not found",
                started_at=start,
                finished_at=utcnow(),
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        data = self._parse_json_output(stdout) if task.output_schema else None

        return AgentResult(
            task_id=task.id,
            backend=self._spec.slug,
            succeeded=proc.returncode == 0,
            text=stdout,
            data=data,
            started_at=start,
            finished_at=utcnow(),
            exit_code=proc.returncode,
            failure_message=stderr[:500] if proc.returncode != 0 else None,
            diagnostics=stderr[-500:] if proc.returncode != 0 else None,
            model=task.model,
        )

    async def stream(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(kind=AgentEventKind.STARTED)
        result = await self.execute(task)
        if result.succeeded:
            yield AgentEvent(kind=AgentEventKind.FINISHED, text=result.text)
        else:
            yield AgentEvent(kind=AgentEventKind.FAILED, text=result.failure_message)

    async def cancel(self, task_id: str) -> bool:
        # The subprocess model does not support cancelling a specific task
        # by id; this is a structural limitation noted for future improvement.
        return False

    def _build_argv(self, task: AgentTask) -> list[str]:
        argv: list[str] = []
        for arg in self._spec.argv_template:
            argv.append(arg.replace("{prompt}", task.instruction))
        if self._spec.json_flag and task.output_schema:
            argv.extend([self._spec.json_flag, "json"])
        if self._spec.model_flag and task.model:
            argv.extend([self._spec.model_flag, task.model])
        return argv

    @staticmethod
    def _parse_json_output(stdout: str) -> dict[str, Any] | None:
        # Try to find a JSON object in the output.
        match = re.search(r"\{.*\}", stdout, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                return None
        return None


def _make_plugin(spec: CliAgentSpec) -> PluginDescriptor[AgentBackend]:
    def factory(settings: Settings) -> CliAgentBackend:
        return CliAgentBackend(settings, spec)

    return agent_plugin(
        slug=spec.slug,
        name=spec.name,
        factory=factory,
        capabilities=_CAPABILITIES,
        description=f"CLI-based {spec.name} agent runtime adapter.",
        priority=spec.priority,
    )


OH_MY_PI = _make_plugin(_SPECS[0])
CODEX = _make_plugin(_SPECS[1])
CLAUDE_CODE = _make_plugin(_SPECS[2])
OPENCODE = _make_plugin(_SPECS[3])
PI = _make_plugin(_SPECS[4])

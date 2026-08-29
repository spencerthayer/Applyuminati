"""External agent runtime contract.

Codex, Claude Code, OpenCode, Pi and Oh My Pi are **execution backends**, not
part of the domain model. Applyuminati delegates a bounded, described task and
receives a structured result; it does not hand them the career profile and
hope.

Adding a runtime means writing an adapter and registering it. No workflow
changes, because workflows request capabilities
(:class:`AgentCapability`) rather than naming a runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory
from applyuminati.core.ids import new_ulid
from applyuminati.core.registry import HealthReport, PluginDescriptor, Registry


class AgentCapability(StrEnum):
    #: Can run a single prompt to completion non-interactively.
    ONESHOT = "oneshot"
    #: Emits incremental events while running.
    STREAMING = "streaming"
    #: Can read and write files in a working directory.
    FILE_ACCESS = "file_access"
    #: Can execute shell commands.
    SHELL = "shell"
    #: Has its own browser tooling we can borrow.
    BROWSER_TOOLS = "browser_tools"
    #: Can return schema-validated structured output.
    STRUCTURED_OUTPUT = "structured_output"
    #: Supports cancellation mid-run.
    CANCELLATION = "cancellation"
    #: Model choice is configurable per invocation.
    MODEL_SELECTION = "model_selection"
    #: Can spawn its own sub-agents.
    SUBAGENTS = "subagents"


class AgentEventKind(StrEnum):
    STARTED = "started"
    OUTPUT = "output"
    TOOL_CALL = "tool_call"
    #: A recoverable problem the runtime reported while working.
    WARNING = "warning"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentEvent(BaseModel):
    """One incremental event from a running agent."""

    model_config = ConfigDict(extra="forbid")

    kind: AgentEventKind
    at: datetime = Field(default_factory=utcnow)
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AgentTask(BaseModel):
    """A bounded unit of work handed to an external runtime."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Logical role this task fulfils: ``researcher``, ``critic``, ``operator``.
    role: str
    #: The instruction. Built from a versioned prompt module, never inline.
    instruction: str
    #: Additional read-only context files the runtime may open.
    context_paths: list[Path] = Field(default_factory=list)
    #: Working directory the runtime is confined to.
    workspace: Path | None = None
    #: JSON Schema the runtime should conform its final answer to.
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    timeout_seconds: float = 600.0
    #: Correlates the delegation with the owning run/task.
    run_id: str | None = None
    parent_task_id: str | None = None


class AgentResult(BaseModel):
    """What a runtime produced, successful or not."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    backend: str
    succeeded: bool
    #: Final text output.
    text: str = ""
    #: Parsed structured output when ``output_schema`` was supplied and honoured.
    data: dict[str, Any] | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    exit_code: int | None = None
    failure_category: FailureCategory | None = None
    failure_message: str | None = None
    #: Stderr tail, truncated and redacted.
    diagnostics: str | None = None
    model: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class AgentMetadata:
    slug: str
    name: str
    capabilities: frozenset[AgentCapability]
    #: Executable looked up on PATH during availability detection.
    executable: str
    platforms: frozenset[str] = field(
        default_factory=lambda: frozenset({"darwin", "linux", "win32"})
    )
    homepage: str | None = None
    notes: str = ""

    def supports(self, capability: AgentCapability) -> bool:
        return capability in self.capabilities


@runtime_checkable
class AgentBackend(Protocol):
    """The interface every external agent runtime adapter implements."""

    @property
    def metadata(self) -> AgentMetadata: ...

    async def health(self) -> HealthReport:
        """Detect whether the runtime is installed and runnable."""
        ...

    async def execute(self, task: AgentTask) -> AgentResult:
        """Run ``task`` to completion. Never raises for runtime failure."""
        ...

    async def stream(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        """Run ``task``, yielding events. Backends without streaming yield
        ``STARTED`` then ``FINISHED``/``FAILED``."""
        ...

    async def cancel(self, task_id: str) -> bool:
        """Request cancellation. Returns whether the request was delivered."""
        ...


#: The process-wide agent backend registry.
AGENT_REGISTRY: Registry[AgentBackend] = Registry("agent", entry_point_group="applyuminati.agents")


def agent_plugin(
    *,
    slug: str,
    name: str,
    factory: Any,  # noqa: ANN401
    capabilities: frozenset[AgentCapability],
    description: str = "",
    priority: int = 0,
) -> PluginDescriptor[AgentBackend]:
    return PluginDescriptor[AgentBackend](
        slug=slug,
        name=name,
        kind="agent",
        factory=factory,
        description=description,
        capabilities=frozenset(c.value for c in capabilities),
        priority=priority,
    )


__all__ = [
    "AGENT_REGISTRY",
    "AgentBackend",
    "AgentCapability",
    "AgentEvent",
    "AgentEventKind",
    "AgentMetadata",
    "AgentResult",
    "AgentTask",
    "agent_plugin",
]

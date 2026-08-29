"""LLM provider contract.

Providers are described by **capabilities**, not by vendor name. Workflows ask
for "a provider that can return structured output" and get one; no workflow
ever imports ``openai`` or branches on ``if provider == "anthropic"``.

Model output is untrusted input (architectural rule 4). The only sanctioned
way to get a typed object out of a model is
:meth:`LLMProvider.complete_structured`, which validates against a Pydantic
schema and raises :class:`InvalidModelOutputError` on failure — it never
returns a half-parsed dict.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.registry import HealthReport, PluginDescriptor, Registry
from applyuminati.core.settings import ProviderConfig

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMCapability(StrEnum):
    CHAT = "chat"
    #: Guarantees schema-conformant output (native JSON schema / tool calling).
    STRUCTURED_OUTPUT = "structured_output"
    #: Best-effort JSON mode without schema enforcement.
    JSON_MODE = "json_mode"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    #: Runs on the user's machine; no data leaves the host.
    LOCAL = "local"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Static description of a provider adapter."""

    slug: str
    name: str
    capabilities: frozenset[LLMCapability]
    #: Models the adapter knows about. Empty means "whatever the user configures".
    known_models: tuple[str, ...] = ()
    #: True when requests leave the local machine.
    remote: bool = True
    homepage: str | None = None
    notes: str = ""

    def supports(self, capability: LLMCapability) -> bool:
        return capability in self.capabilities


class TokenUsage(BaseModel):
    """Token counts, when the provider reports them."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Tokens served from a provider-side cache, where reported.
    cached_input_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def estimated_cost_usd(self, config: ProviderConfig) -> float | None:
        """Cost estimate, or ``None`` when prices are not configured.

        Explicitly an estimate: providers change prices and do not report them
        over the wire, so this is never presented as a billed amount.
        """
        if config.input_cost_per_mtok is None and config.output_cost_per_mtok is None:
            return None
        cost = 0.0
        if self.input_tokens and config.input_cost_per_mtok:
            cost += self.input_tokens / 1_000_000 * config.input_cost_per_mtok
        if self.output_tokens and config.output_cost_per_mtok:
            cost += self.output_tokens / 1_000_000 * config.output_cost_per_mtok
        return cost


class CompletionRequest(BaseModel):
    """One call to a model."""

    model_config = ConfigDict(extra="forbid")

    messages: list[Message]
    #: ``None`` selects the provider's configured default model.
    model: str | None = None
    temperature: float = 0.2
    max_output_tokens: int | None = None
    stop: list[str] = Field(default_factory=list)
    #: JSON Schema the response must satisfy. Set by ``complete_structured``.
    response_schema: dict[str, Any] | None = None
    #: Identifies the prompt that produced these messages, for auditability.
    prompt_id: str | None = None
    prompt_version: str | None = None
    #: Correlates the call with a run/task in the observability log.
    run_id: str | None = None
    task_id: str | None = None
    timeout_seconds: float | None = None

    @property
    def system_prompt(self) -> str | None:
        return next((m.content for m in self.messages if m.role is Role.SYSTEM), None)

    @property
    def conversation(self) -> list[Message]:
        """Messages excluding the system prompt, for providers that split them."""
        return [m for m in self.messages if m.role is not Role.SYSTEM]


class CompletionResponse(BaseModel):
    """A model's reply plus everything needed to audit and cost it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    text: str
    provider: str
    model: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    finish_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    prompt_id: str | None = None
    prompt_version: str | None = None
    #: Provider-specific extras. Redacted before logging.
    raw: dict[str, Any] = Field(default_factory=dict)


class LLMCallRecord(BaseModel):
    """Persistent audit row for one model call, successful or not."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    provider: str
    model: str
    prompt_id: str | None = None
    prompt_version: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    succeeded: bool = True
    failure_category: str | None = None
    failure_message: str | None = None
    #: Number of schema-validation retries this call needed.
    validation_retries: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    """The interface every LLM adapter implements."""

    @property
    def metadata(self) -> ProviderMetadata: ...

    async def health(self) -> HealthReport:
        """Cheap reachability/credential probe."""
        ...

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Free-text completion."""
        ...

    async def complete_structured(
        self, request: CompletionRequest, schema: type[ModelT]
    ) -> tuple[ModelT, CompletionResponse]:
        """Completion validated against ``schema``.

        Raises :class:`~applyuminati.core.errors.InvalidModelOutputError` when
        the model cannot produce conformant output within its retry budget.
        """
        ...

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Token stream. Providers without streaming yield one final chunk."""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """A resolved provider instance plus the configuration it came from."""

    name: str
    provider: LLMProvider
    config: ProviderConfig
    model: str
    #: Capabilities actually requested, for logging.
    required: frozenset[LLMCapability] = field(default_factory=frozenset)


#: The process-wide LLM provider registry.
LLM_REGISTRY: Registry[LLMProvider] = Registry("llm", entry_point_group="applyuminati.llm")


def llm_plugin(
    *,
    slug: str,
    name: str,
    factory: Any,  # noqa: ANN401 - adapter constructors vary
    capabilities: frozenset[LLMCapability],
    description: str = "",
    priority: int = 0,
) -> PluginDescriptor[LLMProvider]:
    return PluginDescriptor[LLMProvider](
        slug=slug,
        name=name,
        kind="llm",
        factory=factory,
        description=description,
        capabilities=frozenset(c.value for c in capabilities),
        requires_auth=LLMCapability.LOCAL not in capabilities,
        priority=priority,
    )


__all__ = [
    "LLM_REGISTRY",
    "CompletionRequest",
    "CompletionResponse",
    "LLMCallRecord",
    "LLMCapability",
    "LLMProvider",
    "Message",
    "ModelT",
    "ProviderMetadata",
    "ProviderSelection",
    "Role",
    "TokenUsage",
    "llm_plugin",
]

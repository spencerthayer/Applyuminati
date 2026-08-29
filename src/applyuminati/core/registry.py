"""Generic plugin registry.

Every extension point in Applyuminati — job sources, browser backends, agent
runtimes, email providers, LLM providers — uses the same registry so they
behave identically: same discovery, same enable/disable semantics, same
health surface, same failure modes.

Design notes:

* Registration is by :class:`PluginDescriptor`, a data object. The registry
  never imports a concrete plugin module; plugins are discovered through
  ``importlib.metadata`` entry points, so a third-party distribution can add
  a source without this repository changing.
* A plugin that fails to load is **recorded**, not swallowed. ``load_errors``
  surfaces in ``applyuminati doctor`` and in ``GET /api/v1/sources``.
* The registry is generic over the plugin protocol, so ``SourceRegistry`` and
  ``BrowserRegistry`` share this implementation without losing type safety.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.errors import ConfigurationError

T = TypeVar("T")


class HealthState(StrEnum):
    """Availability of a backend, reported uniformly across plugin kinds."""

    #: Installed, configured, and responding.
    HEALTHY = "healthy"
    #: Usable but impaired: missing optional config, rate limited, stale auth.
    DEGRADED = "degraded"
    #: Present but cannot be used right now.
    UNAVAILABLE = "unavailable"
    #: Not installed on this host at all.
    NOT_INSTALLED = "not_installed"
    #: Health has not been probed yet.
    UNKNOWN = "unknown"


class HealthReport(BaseModel):
    """The result of probing one plugin."""

    model_config = ConfigDict(extra="forbid")

    plugin: str
    state: HealthState = HealthState.UNKNOWN
    #: One line the user can act on: "ego-browser not on PATH".
    detail: str = ""
    #: Extra facts: version, endpoint, model list. Redacted before logging.
    facts: dict[str, Any] = Field(default_factory=dict)
    checked_at: float | None = None
    latency_ms: float | None = None

    @property
    def usable(self) -> bool:
        return self.state in (HealthState.HEALTHY, HealthState.DEGRADED)


@dataclass(frozen=True, slots=True)
class PluginDescriptor(Generic[T]):
    """Static metadata plus a lazy factory for one plugin.

    The factory is only called when the plugin is actually used, so importing
    the registry never imports ``playwright`` or opens a network client.
    """

    #: Registry-unique slug. Stable: it is written into the database.
    slug: str
    #: Human-readable name for the UI.
    name: str
    #: Extension point family: ``source``, ``browser``, ``agent``, ``email``, ``llm``.
    kind: str
    factory: Callable[..., T]
    description: str = ""
    #: Free-form capability flags consumers can branch on.
    capabilities: frozenset[str] = field(default_factory=frozenset)
    #: Pydantic model validating this plugin's ``options`` block, if any.
    options_schema: type[BaseModel] | None = None
    #: True when the plugin needs credentials before it can run.
    requires_auth: bool = False
    #: Ordering hint; higher wins when several plugins could serve a request.
    priority: int = 0
    #: Where the plugin came from: ``builtin`` or an entry-point distribution.
    origin: str = "builtin"

    def create(self, **kwargs: Any) -> T:  # noqa: ANN401 - plugin-defined kwargs
        return self.factory(**kwargs)

    def validate_options(self, options: dict[str, Any]) -> BaseModel | None:
        """Validate a settings ``options`` block against the plugin's schema."""
        if self.options_schema is None:
            return None
        try:
            return self.options_schema.model_validate(options)
        except Exception as exc:
            raise ConfigurationError(
                f"invalid options for {self.kind} plugin {self.slug!r}: {exc}",
                code="configuration.invalid_plugin_options",
                details={"plugin": self.slug, "kind": self.kind},
            ) from exc


@dataclass(frozen=True, slots=True)
class LoadError:
    """A plugin that could not be imported. Surfaced, never silently dropped."""

    slug: str
    kind: str
    origin: str
    message: str


class Registry(Generic[T]):
    """A named collection of plugin descriptors for one extension point."""

    def __init__(self, kind: str, *, entry_point_group: str | None = None) -> None:
        self.kind = kind
        self.entry_point_group = entry_point_group or f"applyuminati.{kind}s"
        self._plugins: dict[str, PluginDescriptor[T]] = {}
        self._load_errors: list[LoadError] = []
        self._discovered = False

    # -- registration -----------------------------------------------------

    def register(self, descriptor: PluginDescriptor[T], *, replace: bool = False) -> None:
        if descriptor.kind != self.kind:
            msg = (
                f"cannot register {descriptor.slug!r} of kind {descriptor.kind!r} "
                f"in the {self.kind!r} registry"
            )
            raise ConfigurationError(msg, code="configuration.plugin_kind_mismatch")
        if descriptor.slug in self._plugins and not replace:
            msg = f"{self.kind} plugin {descriptor.slug!r} is already registered"
            raise ConfigurationError(msg, code="configuration.duplicate_plugin")
        self._plugins[descriptor.slug] = descriptor

    def unregister(self, slug: str) -> None:
        self._plugins.pop(slug, None)

    # -- discovery --------------------------------------------------------

    def discover(self, *, force: bool = False) -> None:
        """Load descriptors advertised through entry points. Idempotent."""
        if self._discovered and not force:
            return
        self._discovered = True
        for entry_point in entry_points(group=self.entry_point_group):
            self._load_entry_point(entry_point)

    def _load_entry_point(self, entry_point: EntryPoint) -> None:
        origin = getattr(entry_point.dist, "name", "unknown") if entry_point.dist else "unknown"
        try:
            loaded = entry_point.load()
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not break startup
            self._load_errors.append(
                LoadError(
                    slug=entry_point.name, kind=self.kind, origin=origin, message=str(exc)
                )
            )
            return
        if not isinstance(loaded, PluginDescriptor):
            self._load_errors.append(
                LoadError(
                    slug=entry_point.name,
                    kind=self.kind,
                    origin=origin,
                    message=(
                        f"entry point resolved to {type(loaded).__name__}, "
                        "expected a PluginDescriptor"
                    ),
                )
            )
            return
        if loaded.slug in self._plugins:
            return
        self.register(loaded)

    @property
    def load_errors(self) -> list[LoadError]:
        return list(self._load_errors)

    # -- lookup -----------------------------------------------------------

    def get(self, slug: str) -> PluginDescriptor[T]:
        self.discover()
        try:
            return self._plugins[slug]
        except KeyError as exc:
            msg = (
                f"unknown {self.kind} plugin {slug!r}; "
                f"registered: {sorted(self._plugins) or '(none)'}"
            )
            raise ConfigurationError(
                msg,
                code="configuration.unknown_plugin",
                details={"kind": self.kind, "slug": slug},
            ) from exc

    def try_get(self, slug: str) -> PluginDescriptor[T] | None:
        self.discover()
        return self._plugins.get(slug)

    def __contains__(self, slug: object) -> bool:
        self.discover()
        return slug in self._plugins

    def __iter__(self) -> Iterator[PluginDescriptor[T]]:
        self.discover()
        return iter(sorted(self._plugins.values(), key=lambda p: (-p.priority, p.slug)))

    def __len__(self) -> int:
        self.discover()
        return len(self._plugins)

    def slugs(self) -> list[str]:
        self.discover()
        return sorted(self._plugins)

    def all(self) -> list[PluginDescriptor[T]]:
        return list(self)

    def with_capability(self, capability: str) -> list[PluginDescriptor[T]]:
        return [p for p in self if capability in p.capabilities]

    def clear(self) -> None:
        """Reset the registry. Test affordance only."""
        self._plugins.clear()
        self._load_errors.clear()
        self._discovered = False


__all__ = [
    "HealthReport",
    "HealthState",
    "LoadError",
    "PluginDescriptor",
    "Registry",
]

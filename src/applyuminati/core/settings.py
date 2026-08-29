"""Typed configuration.

Precedence, strongest first:

1. Explicit keyword arguments (used by tests and by CLI flags).
2. Environment variables, prefixed ``APPLYUMINATI_`` and nested with ``__``
   (e.g. ``APPLYUMINATI_LLM__DEFAULT_PROVIDER=ollama``).
3. A ``.env`` file in the working directory.
4. ``config.toml`` inside the data directory.
5. Field defaults.

Every secret is a :class:`~pydantic.SecretStr`, so it cannot be printed,
serialised into an API response, or written into a log by accident.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from applyuminati.core.errors import ConfigurationError

DEFAULT_DATA_DIR = Path.home() / ".applyuminati"
CONFIG_FILENAME = "config.toml"


class ExecutionMode(StrEnum):
    """How far Applyuminati is permitted to go without asking.

    ``AUTONOMOUS_SUBMIT`` is a first-class supported mode, but it is never the
    default: enabling it is an explicit, recorded configuration act.
    """

    RESEARCH_ONLY = "research_only"
    PREPARE_APPLICATION = "prepare_application"
    FILL_NO_SUBMIT = "fill_no_submit"
    AUTONOMOUS_SUBMIT = "autonomous_submit"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class ProviderConfig(BaseModel):
    """One registered LLM provider instance.

    The ``kind`` selects a registered adapter (``openai_compatible``,
    ``anthropic``, ``gemini``, …). Because OpenAI, OpenRouter, Ollama, vLLM,
    LM Studio and most self-hosted gateways all speak the same wire format,
    they are all ``openai_compatible`` with a different ``base_url``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "openai_compatible"
    enabled: bool = True
    base_url: str | None = None
    api_key: SecretStr | None = None
    #: Model used when a task does not request a specific one.
    default_model: str | None = None
    #: Model preferred for cheap, high-volume calls (classification, extraction).
    fast_model: str | None = None
    #: Optional per-1M-token prices, used for cost estimation only.
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 2
    extra_headers: dict[str, str] = Field(default_factory=dict)


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Name of the entry in :attr:`providers` used when nothing else is specified.
    default_provider: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    #: Hard ceiling on spend per discovery/scoring run, in USD. ``None`` disables.
    run_budget_usd: float | None = None
    #: When false, every optional LLM enrichment pass is skipped and the system
    #: falls back to deterministic behaviour. Useful for offline operation.
    enabled: bool = True

    def resolved_default(self) -> str | None:
        if self.default_provider:
            return self.default_provider
        enabled = [name for name, cfg in self.providers.items() if cfg.enabled]
        return enabled[0] if len(enabled) == 1 else None


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Ordered preference list. The first *available* backend wins, so a host
    #: without ego lite silently falls back to Playwright.
    preferred: list[str] = Field(default_factory=lambda: ["ego_lite", "playwright"])
    headless: bool = True
    #: Absolute path to the ``ego-browser`` helper. When unset it is discovered
    #: on PATH and inside the ``ego lite.app`` bundle. ego lite is driven as a
    #: subprocess (``ego-browser nodejs`` reading a script on stdin); it does
    #: not expose a CDP port, so there is no URL to configure.
    ego_lite_binary: str | None = None
    #: Overrides ``EGO_BROWSER_AGENT_WORKSPACE``: where ego lite loads per-site
    #: helper packs from. Defaults to ``<data_dir>/ego-workspace``.
    ego_lite_workspace: Path | None = None
    #: Persisted Playwright ``storage_state`` file, so logins survive runs.
    playwright_storage_state: Path | None = None
    navigation_timeout_seconds: float = 45.0
    #: Screenshots and DOM captures written per application attempt.
    capture_artifacts: bool = True


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Ordered preference list of external agent runtimes.
    preferred: list[str] = Field(default_factory=lambda: ["oh_my_pi", "codex", "claude_code"])
    enabled: bool = False
    default_timeout_seconds: float = 600.0
    #: Per-backend executable overrides, keyed by backend slug.
    binaries: dict[str, str] = Field(default_factory=dict)


class EmailAccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "imap"
    enabled: bool = False
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: SecretStr | None = None
    use_ssl: bool = True
    mailbox: str = "INBOX"


class EmailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: dict[str, EmailAccountConfig] = Field(default_factory=dict)


class SourceConfig(BaseModel):
    """Per-source enablement and plugin-defined options.

    ``options`` is deliberately untyped here: each plugin declares its own
    Pydantic schema and validates its slice at registration time, so core
    settings never need to know what a Greenhouse board token is.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    #: Global politeness floor applied on top of each plugin's own rate limit.
    min_request_interval_seconds: float = 0.5
    user_agent: str = (
        "Applyuminati/0.1 (+https://github.com/spencerthayer/Applyuminati) local-first job search"
    )
    request_timeout_seconds: float = 30.0
    #: Respect robots.txt for HTML scraping strategies.
    respect_robots_txt: bool = True


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8000
    #: Extra browser origins allowed to call the API. The bundled UI is served
    #: same-origin, so this stays empty in the default Docker deployment.
    cors_origins: list[str] = Field(default_factory=list)
    #: Directory of built web assets. Served at ``/`` when present.
    web_dist: Path | None = None


class Settings(BaseSettings):
    """Root configuration object. Constructed once and injected."""

    model_config = SettingsConfigDict(
        env_prefix="APPLYUMINATI_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        nested_model_default_partial_update=True,
    )

    #: Everything local lives here: SQLite file, artifacts, config.toml, logs.
    data_dir: Path = DEFAULT_DATA_DIR
    #: Overrides the derived SQLite URL. Keep the dialect SQLAlchemy-supported:
    #: swapping in ``postgresql+psycopg://`` later must not require domain changes.
    database_url: str | None = None
    environment: Literal["local", "docker", "ci"] = "local"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    execution_mode: ExecutionMode = ExecutionMode.RESEARCH_ONLY

    server: ServerSettings = Field(default_factory=ServerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    discovery: SourceSettings = Field(default_factory=SourceSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _TomlConfigSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        default = self.llm.default_provider
        if default and default not in self.llm.providers:
            msg = (
                f"llm.default_provider={default!r} is not a configured provider; "
                f"known providers: {sorted(self.llm.providers) or '(none)'}"
            )
            raise ValueError(msg)
        if self.execution_mode is ExecutionMode.AUTONOMOUS_SUBMIT and not self.browser.preferred:
            msg = "execution_mode=autonomous_submit requires at least one browser backend"
            raise ValueError(msg)
        return self

    # -- derived paths ----------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / "applyuminati.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILENAME

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.artifacts_dir, self.documents_dir):
            path.mkdir(parents=True, exist_ok=True)

    def enabled_sources(self) -> list[str]:
        return sorted(name for name, cfg in self.discovery.sources.items() if cfg.enabled)

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to return from the API: secrets replaced by a flag."""
        payload = self.model_dump(mode="json", exclude={"llm", "email"})
        payload["llm"] = {
            "enabled": self.llm.enabled,
            "default_provider": self.llm.default_provider,
            "run_budget_usd": self.llm.run_budget_usd,
            "providers": {
                name: {
                    "kind": cfg.kind,
                    "enabled": cfg.enabled,
                    "base_url": cfg.base_url,
                    "default_model": cfg.default_model,
                    "fast_model": cfg.fast_model,
                    "has_api_key": cfg.api_key is not None,
                }
                for name, cfg in self.llm.providers.items()
            },
        }
        payload["email"] = {
            "accounts": {
                name: {
                    "kind": cfg.kind,
                    "enabled": cfg.enabled,
                    "host": cfg.host,
                    "username": cfg.username,
                    "has_password": cfg.password is not None,
                }
                for name, cfg in self.email.accounts.items()
            }
        }
        return payload


class _TomlConfigSource(PydanticBaseSettingsSource):
    """Loads ``config.toml`` from the resolved data directory.

    Resolved independently of the ``Settings`` instance being built, because
    the data directory itself can be overridden by env var.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: ANN401
        raise NotImplementedError  # pragma: no cover - not used by __call__

    def __call__(self) -> dict[str, Any]:
        import os

        raw_dir = os.environ.get("APPLYUMINATI_DATA_DIR")
        data_dir = Path(raw_dir).expanduser() if raw_dir else DEFAULT_DATA_DIR
        path = data_dir / CONFIG_FILENAME
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(
                f"could not read configuration file {path}: {exc}",
                code="configuration.bad_config_file",
                details={"path": str(path)},
            ) from exc


_settings: Settings | None = None


def get_settings(**overrides: Any) -> Settings:  # noqa: ANN401
    """Return the process settings, building them on first use.

    Passing overrides always constructs a fresh instance (used by tests and by
    CLI flags) and does not disturb the cached one.
    """
    global _settings  # noqa: PLW0603 - deliberate process-wide singleton
    if overrides:
        return Settings(**overrides)
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace (or clear) the cached process settings."""
    global _settings  # noqa: PLW0603
    _settings = settings


def default_source_settings(names: Sequence[str]) -> dict[str, SourceConfig]:
    """Helper for ``applyuminati init``: every known source, disabled."""
    return {name: SourceConfig(enabled=False) for name in names}


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_DATA_DIR",
    "AgentSettings",
    "BrowserSettings",
    "EmailAccountConfig",
    "EmailSettings",
    "ExecutionMode",
    "LLMSettings",
    "LogFormat",
    "ProviderConfig",
    "ServerSettings",
    "Settings",
    "SourceConfig",
    "SourceSettings",
    "default_source_settings",
    "get_settings",
    "set_settings",
]

"""Settings and strategy.

Two rules enforced here:

* **Secrets never leave the process.** Provider configuration is exposed as
  metadata plus ``has_api_key``; :meth:`Settings.public_dict` does the same for
  everything else.
* **Strategy is stored as exact numbers.** Choosing the "wide" preset
  materialises concrete values into the profile immediately, so what the user
  later sees and tunes is the real configuration, not a label that silently
  means something different after an upgrade.
"""

from __future__ import annotations

from typing import Any

from applyuminati.core.errors import ConfigurationError
from applyuminati.core.settings import Settings
from applyuminati.core.strategy import PRESETS, SearchStrategy, preset
from applyuminati.services.container import Repositories


class SettingsService:
    def __init__(self, repos: Repositories, settings: Settings) -> None:
        self._repos = repos
        self._settings = settings

    async def snapshot(self) -> dict[str, Any]:
        """Redaction-safe view of the runtime configuration."""
        profile = await self._repos.profiles.get_active()
        strategy = profile.strategy if profile else SearchStrategy()
        return {
            "execution_mode": self._settings.execution_mode.value,
            "data_dir": str(self._settings.data_dir),
            "database": _redact_database_url(self._settings.resolved_database_url),
            "log_level": self._settings.log_level,
            "llm_enabled": self._settings.llm.enabled,
            "default_provider": self._settings.llm.default_provider,
            "providers": [
                {
                    "name": name,
                    "kind": cfg.kind,
                    "enabled": cfg.enabled,
                    "base_url": cfg.base_url,
                    "default_model": cfg.default_model,
                    "fast_model": cfg.fast_model,
                    "has_api_key": cfg.api_key is not None,
                }
                for name, cfg in self._settings.llm.providers.items()
            ],
            "browser_preferred": list(self._settings.browser.preferred),
            "agents_enabled": self._settings.agents.enabled,
            "agents_preferred": list(self._settings.agents.preferred),
            "email_accounts": sorted(self._settings.email.accounts),
            "strategy": strategy,
        }

    async def strategy(self) -> SearchStrategy:
        profile = await self._repos.profiles.get_active()
        return profile.strategy if profile else SearchStrategy()

    async def update_strategy(
        self, *, strategy: SearchStrategy | None = None, preset_name: str | None = None
    ) -> SearchStrategy:
        """Persist a new strategy, from explicit values or a materialised preset."""
        if strategy is None and preset_name is None:
            raise ConfigurationError(
                "supply either an explicit strategy or a preset name",
                code="configuration.missing_strategy",
            )
        if preset_name is not None and preset_name not in PRESETS:
            raise ConfigurationError(
                f"unknown strategy preset {preset_name!r}; known: {sorted(PRESETS)}",
                code="configuration.unknown_preset",
            )

        resolved = strategy if strategy is not None else preset(preset_name or "balanced")

        profile = await self._repos.profiles.get_active()
        if profile is None:
            raise ConfigurationError(
                "import a profile before configuring a search strategy",
                code="configuration.profile_missing",
            )
        updated = profile.model_copy(update={"strategy": resolved})
        await self._repos.profiles.upsert(updated)
        return resolved

    def presets(self) -> dict[str, SearchStrategy]:
        return {name: value.model_copy(deep=True) for name, value in PRESETS.items()}


def _redact_database_url(url: str) -> str:
    """Strip any embedded credentials before showing a database URL."""
    if "@" not in url:
        return url
    scheme, _, remainder = url.partition("://")
    _, _, host = remainder.rpartition("@")
    return f"{scheme}://***@{host}"


__all__ = ["SettingsService"]

"""Profile import, export and preferences.

Thin: the interesting work (deriving the claim ledger, extracting metrics) is
in :mod:`applyuminati.resume.importer`. This service owns persistence and the
policy question of what happens when a profile already exists — it refuses to
clobber one silently, because a career profile is the user's most valuable
local data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from applyuminati.core.errors import ConfigurationError, NotFoundError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.common import Compensation, EmploymentType, RemoteMode, SeniorityLevel
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.strategy import SearchStrategy
from applyuminati.resume.exporter import export_json_resume
from applyuminati.resume.importer import import_json_resume
from applyuminati.services.container import Repositories
from applyuminati.services.views import ImportResult, ProfileView

log = get_logger(__name__)


def _profile_view(profile: CareerProfile) -> ProfileView:
    basics = profile.resume.basics
    claim_levels: dict[str, int] = {}
    for claim in profile.claims:
        claim_levels[claim.level.value] = claim_levels.get(claim.level.value, 0) + 1
    return ProfileView(
        profile_id=profile.id,
        label=profile.label,
        resume=profile.resume.to_json_dict(),
        name=basics.name,
        headline=basics.label,
        email=basics.email,
        counts={
            "work": len(profile.resume.work),
            "education": len(profile.resume.education),
            "skills": len(profile.resume.skills),
            "projects": len(profile.resume.projects),
            "certificates": len(profile.resume.certificates),
            "publications": len(profile.resume.publications),
            "volunteer": len(profile.resume.volunteer),
            "awards": len(profile.resume.awards),
            "claims": len(profile.claims),
            "metrics": len(profile.metrics),
            "stories": len(profile.stories),
            "artifacts": len(profile.artifacts),
            "questionnaire_defaults": len(profile.questionnaire_defaults),
        },
        claim_levels=claim_levels,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


class ProfileService:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    async def get(self) -> CareerProfile:
        profile = await self._repos.profiles.get_active()
        if profile is None:
            raise NotFoundError(
                "no career profile has been imported yet",
                code="resource_gone.profile_missing",
            )
        return profile

    async def try_get(self) -> CareerProfile | None:
        return await self._repos.profiles.get_active()

    async def view(self) -> ProfileView:
        return _profile_view(await self.get())

    async def import_resume(
        self,
        payload: dict[str, Any],
        *,
        label: str = "default",
        replace: bool = False,
        origin: str = "api",
    ) -> ImportResult:
        """Import a JSON Resume document and derive the claim ledger."""
        existing = await self._repos.profiles.get_active()
        if existing is not None and not replace:
            raise ConfigurationError(
                "a profile already exists; pass replace=true to overwrite it",
                code="configuration.profile_exists",
                details={"existing_label": existing.label},
            )

        profile, warnings = import_json_resume(payload, label=label, origin=origin)
        if existing is not None:
            # Preserve identity and learned preferences across a re-import: the
            # resume is the input, but wording preferences, questionnaire
            # defaults and stories are accumulated knowledge the user would be
            # furious to lose to a routine resume refresh.
            profile = profile.model_copy(
                update={
                    "id": existing.id,
                    "created_at": existing.created_at,
                    "stories": existing.stories,
                    "wording_preferences": existing.wording_preferences,
                    "questionnaire_defaults": existing.questionnaire_defaults,
                    "writing_style": existing.writing_style,
                    "strategy": existing.strategy,
                    "targets": profile.targets if profile.targets.titles else existing.targets,
                }
            )

        saved = await self._repos.profiles.upsert(profile)
        log.info(
            "profile.imported",
            profile_id=saved.id,
            claims=len(saved.claims),
            metrics=len(saved.metrics),
            warnings=len(warnings),
        )
        return ImportResult(
            profile=_profile_view(saved),
            claims_created=len(saved.claims),
            metrics_extracted=len(saved.metrics),
            warnings=warnings,
        )

    async def import_from_path(self, path: Path, *, replace: bool = False) -> ImportResult:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"could not read JSON Resume from {path}: {exc}",
                code="configuration.bad_resume_file",
                details={"path": str(path)},
            ) from exc
        if not isinstance(payload, dict):
            raise ConfigurationError(
                f"{path} does not contain a JSON object",
                code="configuration.bad_resume_file",
            )
        return await self.import_resume(payload, replace=replace, origin=str(path))

    async def export_resume(self) -> dict[str, Any]:
        return export_json_resume(await self.get())

    async def update_preferences(
        self,
        *,
        titles: list[str] | None = None,
        locations: list[str] | None = None,
        remote_modes: list[RemoteMode] | None = None,
        employment_types: list[EmploymentType] | None = None,
        seniority: SeniorityLevel | None = None,
        minimum_compensation: float | None = None,
        compensation_currency: str | None = None,
        strategy: SearchStrategy | None = None,
    ) -> CareerProfile:
        """Update search targets and strategy. Only supplied fields change."""
        from applyuminati.core.models.common import Location

        profile = await self.get()
        targets = profile.targets.model_copy(deep=True)
        if titles is not None:
            targets.titles = titles
        if locations is not None:
            targets.locations = [Location(raw=value) for value in locations]
        if remote_modes is not None:
            targets.remote_modes = remote_modes
        if employment_types is not None:
            targets.employment_types = employment_types
        if seniority is not None:
            targets.seniority = seniority
        if minimum_compensation is not None:
            targets.compensation_floor = Compensation(
                minimum=minimum_compensation,
                currency=compensation_currency or "USD",
            )

        updated = profile.model_copy(
            update={
                "targets": targets,
                "strategy": strategy or profile.strategy,
            }
        )
        return await self._repos.profiles.upsert(updated)

    async def search_queries(self, override: list[str] | None = None) -> list[str]:
        """Titles to search for: the caller's override, else the profile targets."""
        if override:
            return override
        profile = await self.try_get()
        if profile is None or not profile.targets.titles:
            return []
        return profile.targets.titles


__all__ = ["ProfileService"]

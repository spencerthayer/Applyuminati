"""Browser backend selection: capability first, preference second.

Selection used to be "walk ``settings.browser.preferred``, take the first thing
that answers a health probe". That is fine for reading a job posting and wrong
for submitting an application, because it will happily hand an authenticated
Workday portal to a headless container that has never been signed in anywhere.

So the order is inverted. Requirements disqualify; preference only ranks:

1. Drop backends this platform cannot run.
2. Drop backends missing a *required* capability. This is a veto, not a
   downgrade, and it is why there is no silent fallback.
3. Drop backends that fail their health probe.
4. Among what is left, take the earliest in ``settings.browser.preferred``,
   breaking ties by how many *preferred* capabilities the backend also covers.

When nothing qualifies the error names each backend and the specific reason,
because "no browser available" is not something a user can act on and
"ego_lite: macOS only, this host is linux; playwright: cannot hand control to a
human" is.

Backends outside the preference list are still considered, ranked after
everything in it. An operator who installs a capable backend and forgets to list
it should get a working application, not a puzzling refusal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from applyuminati.browser.base import (
    BROWSER_REGISTRY,
    BrowserBackend,
    BrowserCapability,
    BrowserMetadata,
)
from applyuminati.browser.capabilities import (
    READ_ONLY_INSPECTION,
    BackendCandidate,
    BrowserRequirements,
)
from applyuminati.core.errors import BackendUnavailableError
from applyuminati.core.logging import get_logger
from applyuminati.core.platform import current_platform
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import Settings

log = get_logger(__name__)

__all__ = ["evaluate_backends", "probe_all", "select_browser"]


def _rank(slug: str, preferred: list[str]) -> int:
    """Position in the preference list; unlisted backends sort last."""
    return preferred.index(slug) if slug in preferred else len(preferred)


@dataclass(slots=True)
class _Verdict:
    """Mutable accumulator, so ``_evaluate`` has one exit and one construction."""

    backend: BrowserBackend | None = None
    metadata: BrowserMetadata | None = None
    health: HealthReport | None = None
    missing: frozenset[BrowserCapability] = frozenset()
    rejection: str | None = None


def _capability_rejection(
    metadata: BrowserMetadata,
    requirements: BrowserRequirements,
    *,
    platform: str,
) -> tuple[str, frozenset[BrowserCapability]] | None:
    """Reject on static metadata alone, before paying for a health probe.

    Probing ego lite spawns a subprocess. There is no point doing that to learn
    something the declared platform and capability set already answered.
    """
    if platform not in metadata.platforms:
        supported = ", ".join(sorted(metadata.platforms))
        return f"runs on {supported}, not {platform}", frozenset()
    missing = requirements.missing_from(metadata)
    if missing:
        names = ", ".join(sorted(c.value for c in missing))
        return f"cannot {names}", missing
    return None


async def _verdict_for(
    descriptor_create: Callable[..., BrowserBackend],
    settings: Settings,
    requirements: BrowserRequirements,
    *,
    platform: str,
) -> _Verdict:
    verdict = _Verdict()
    try:
        verdict.backend = descriptor_create(settings=settings)
        verdict.metadata = verdict.backend.metadata
    except Exception as exc:
        verdict.rejection = f"could not be constructed: {exc}"
        return verdict

    static = _capability_rejection(verdict.metadata, requirements, platform=platform)
    if static is not None:
        verdict.rejection, verdict.missing = static
        return verdict

    try:
        verdict.health = await verdict.backend.health()
    except Exception as exc:
        verdict.rejection = f"health probe raised: {exc}"
        return verdict

    if not verdict.health.usable:
        verdict.rejection = f"{verdict.health.state.value} — {verdict.health.detail}"
    return verdict


async def _evaluate(
    slug: str,
    settings: Settings,
    requirements: BrowserRequirements,
    *,
    platform: str,
) -> BackendCandidate:
    """Construct, capability-check and health-probe one backend."""
    descriptor = BROWSER_REGISTRY.try_get(slug)
    if descriptor is None:
        verdict = _Verdict(rejection="not registered")
    elif requirements.backend_slug is not None and requirements.backend_slug != slug:
        verdict = _Verdict(rejection=f"not the requested backend ({requirements.backend_slug})")
    else:
        verdict = await _verdict_for(descriptor.create, settings, requirements, platform=platform)

    eligible = verdict.rejection is None and verdict.metadata is not None
    return BackendCandidate(
        slug=slug,
        backend=verdict.backend,
        metadata=verdict.metadata,
        health=verdict.health,
        missing=verdict.missing,
        preference_score=(
            requirements.preference_score(verdict.metadata)
            if eligible and verdict.metadata is not None
            else 0
        ),
        preference_rank=_rank(slug, settings.browser.preferred),
        rejection=verdict.rejection,
    )


async def evaluate_backends(
    settings: Settings,
    requirements: BrowserRequirements | None = None,
) -> list[BackendCandidate]:
    """Evaluate every registered backend, best first.

    Rejected candidates are included and carry their reason, so a caller can
    explain a refusal without probing everything a second time.
    """
    needs = requirements or READ_ONLY_INSPECTION
    platform = current_platform()
    slugs = sorted(set(BROWSER_REGISTRY.slugs()) | set(settings.browser.preferred))
    candidates = await asyncio.gather(
        *(_evaluate(slug, settings, needs, platform=platform) for slug in slugs)
    )
    # Eligible first, then preference order, then most nice-to-haves covered.
    return sorted(
        candidates,
        key=lambda c: (not c.eligible, c.preference_rank, -c.preference_score, c.slug),
    )


async def select_browser(
    settings: Settings,
    requirements: BrowserRequirements | None = None,
) -> tuple[BrowserBackend, HealthReport]:
    """Return the best backend satisfying ``requirements``, and its health.

    Raises :class:`BackendUnavailableError` rather than returning a backend that
    is merely close. A workflow needing human handoff and getting a backend
    without it does not degrade gracefully; it fails later, further in, with a
    partly filled application nobody asked for.
    """
    needs = requirements or READ_ONLY_INSPECTION
    candidates = await evaluate_backends(settings, needs)
    chosen = next((c for c in candidates if c.eligible), None)

    if chosen is None or chosen.backend is None or chosen.health is None:
        rejections = [c.describe() for c in candidates]
        raise BackendUnavailableError(
            f"no browser backend satisfies {needs.describe()}; " + "; ".join(rejections),
            code="browser.none_available",
            details={
                "requirements": {
                    "required": sorted(c.value for c in needs.required),
                    "preferred": sorted(c.value for c in needs.preferred),
                    "backend_slug": needs.backend_slug,
                },
                "rejections": rejections,
                "preferred": list(settings.browser.preferred),
                "platform": current_platform(),
            },
        )

    log.info(
        "browser.selected",
        backend=chosen.slug,
        preference_rank=chosen.preference_rank,
        preferred_covered=chosen.preference_score,
        rejected=[c.describe() for c in candidates if not c.eligible],
    )
    return chosen.backend, chosen.health


async def probe_all(settings: Settings) -> list[HealthReport]:
    """Probe every registered browser backend concurrently."""

    async def probe(slug: str) -> HealthReport:
        descriptor = BROWSER_REGISTRY.try_get(slug)
        if descriptor is None:
            return HealthReport(
                plugin=slug, state=HealthState.NOT_INSTALLED, detail="not registered"
            )
        try:
            backend = descriptor.create(settings=settings)
            return await backend.health()
        except Exception as exc:
            return HealthReport(plugin=slug, state=HealthState.UNAVAILABLE, detail=str(exc))

    return list(await asyncio.gather(*(probe(slug) for slug in BROWSER_REGISTRY.slugs())))

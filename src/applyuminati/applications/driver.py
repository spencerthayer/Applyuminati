"""ApplicationDriver contract.

Discovery ends at a normalised job. Execution begins from the application URL.
A driver owns ATS detection hints, step interpretation, question extraction,
form filling, upload, validation, submission, verification and recovery. A
source owns none of that.

Concrete drivers live in :mod:`applyuminati.plugins.applications`. This package
never imports them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from applyuminati.applications.detect import Detection, detect_ats
from applyuminati.browser.base import BrowserSession, PageObservation
from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    AttemptFailure,
    HumanIntervention,
    SubmissionEvidence,
)
from applyuminati.core.models.job import AtsVendor, Job
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.registry import PluginDescriptor, PluginMaturity, Registry
from applyuminati.core.settings import ExecutionMode

__all__ = [
    "APPLICATION_DRIVER_REGISTRY",
    "ApplicationDriver",
    "DriverContext",
    "DriverError",
    "DriverMetadata",
    "DriverOutcome",
    "DriverOutcomeKind",
    "application_driver",
    "detect_driver",
]


class DriverOutcomeKind(StrEnum):
    CONTINUED = "continued"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class DriverOutcome:
    kind: DriverOutcomeKind
    attempt: ApplicationAttempt
    intervention: HumanIntervention | None = None
    evidence: SubmissionEvidence | None = None
    failure: AttemptFailure | None = None


@dataclass(frozen=True, slots=True)
class DriverMetadata:
    slug: str
    name: str
    ats: AtsVendor
    version: str = "1"
    #: Host suffixes this driver claims. Detection uses these before a page load.
    hosts: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class DriverContext:
    """Everything a driver may read. Nothing it may write except through attempt."""

    job: Job
    profile: CareerProfile
    mode: ExecutionMode
    documents: dict[str, Path] = field(default_factory=dict)
    observation: PageObservation | None = None


class DriverError(ApplyuminatiError):
    category = FailureCategory.EXTRACTION_DRIFT


@runtime_checkable
class ApplicationDriver(Protocol):
    """One ATS (or employer-specific) application workflow."""

    @property
    def metadata(self) -> DriverMetadata: ...

    def detects(self, url: str) -> Detection:
        """Confidence that this driver owns ``url``. 0 means no."""
        ...

    async def run(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> DriverOutcome:
        """Advance the attempt from its latest checkpoint.

        Must inspect current state before repeating a consequential action.
        If ``attempt.submission_attempted_at`` is set, this may only verify.
        """
        ...


APPLICATION_DRIVER_REGISTRY: Registry[ApplicationDriver] = Registry(
    "application_driver", entry_point_group="applyuminati.application_drivers"
)


def application_driver(
    *,
    slug: str,
    name: str,
    factory: Any,
    ats: AtsVendor,
    description: str = "",
    priority: int = 0,
    maturity: PluginMaturity = PluginMaturity.ADAPTER_EXISTS,
) -> PluginDescriptor[ApplicationDriver]:
    return PluginDescriptor[ApplicationDriver](
        slug=slug,
        name=name,
        kind="application_driver",
        factory=factory,
        description=description,
        capabilities=frozenset({ats.value}),
        priority=priority,
        maturity=maturity,
    )


def detect_driver(
    url: str,
    drivers: Sequence[ApplicationDriver] | None = None,
) -> tuple[ApplicationDriver, Detection] | None:
    """Pick the driver with the highest detection confidence above zero.

    The application URL decides. Discovery source is not an argument.
    """
    candidates = list(drivers) if drivers is not None else _loaded_drivers()
    best: tuple[ApplicationDriver, Detection] | None = None
    for driver in candidates:
        detection = driver.detects(url)
        if detection.confidence <= 0:
            continue
        if best is None or detection.confidence > best[1].confidence:
            best = (driver, detection)
    if best is None:
        fallback = detect_ats(url)
        if fallback.ats is not AtsVendor.UNKNOWN:
            for driver in candidates:
                if driver.metadata.ats is fallback.ats:
                    return driver, fallback
    return best


def _loaded_drivers() -> list[ApplicationDriver]:
    APPLICATION_DRIVER_REGISTRY.discover()
    drivers: list[ApplicationDriver] = []
    for slug in APPLICATION_DRIVER_REGISTRY.slugs():
        descriptor = APPLICATION_DRIVER_REGISTRY.get(slug)
        drivers.append(descriptor.create())
    return drivers

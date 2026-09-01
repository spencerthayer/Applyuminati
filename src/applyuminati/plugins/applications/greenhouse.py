"""Greenhouse ApplicationDriver.

Proves the execution architecture: ATS detection from the apply URL, attempt
creation, capability-matched browser use, questionnaire policy, HITL pause,
checkpoint resume, and submission evidence. Tests drive a fake session, never
a real employer.
"""

from __future__ import annotations

from applyuminati.applications.detect import Detection, detect_ats
from applyuminati.applications.driver import (
    DriverContext,
    DriverMetadata,
    DriverOutcome,
    application_driver,
)
from applyuminati.applications.runner import run_form_application
from applyuminati.browser.base import BrowserSession, ElementRole, PageElement
from applyuminati.core.models.execution import ApplicationAttempt
from applyuminati.core.models.job import AtsVendor
from applyuminati.core.registry import PluginMaturity

SLUG = "greenhouse"
VERSION = "1"

METADATA = DriverMetadata(
    slug=SLUG,
    name="Greenhouse",
    ats=AtsVendor.GREENHOUSE,
    version=VERSION,
    hosts=frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io"}),
)


def _is_submit(element: PageElement) -> bool:
    return (
        element.role is ElementRole.BUTTON
        and bool(element.label)
        and "submit" in element.label.lower()
    )


class GreenhouseDriver:
    @property
    def metadata(self) -> DriverMetadata:
        return METADATA

    def detects(self, url: str) -> Detection:
        detection = detect_ats(url)
        if detection.ats is AtsVendor.GREENHOUSE:
            return detection
        return Detection(AtsVendor.UNKNOWN, confidence=0.0, host=detection.host)

    async def run(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> DriverOutcome:
        url = context.job.apply_url or context.job.canonical_url
        return await run_form_application(
            attempt,
            session,
            context,
            slug=SLUG,
            version=VERSION,
            started_message="greenhouse application opened",
            apply_url=url,
            is_submit=_is_submit,
        )


def _create() -> GreenhouseDriver:
    return GreenhouseDriver()


PLUGIN = application_driver(
    slug=SLUG,
    name=METADATA.name,
    factory=_create,
    ats=AtsVendor.GREENHOUSE,
    description="Greenhouse application workflow. Detection is from the apply URL.",
    priority=50,
    maturity=PluginMaturity.WORKFLOW_INTEGRATED,
)

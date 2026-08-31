"""Lever ApplicationDriver.

Second driver on purpose: if ApplicationDriver were a Greenhouse interface
with a generic name, Lever would force conditionals into core. Lever uses the
same runner helpers and its own detection, submit-button labels, and
confirmation markers.
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

SLUG = "lever"
VERSION = "1"

METADATA = DriverMetadata(
    slug=SLUG,
    name="Lever",
    ats=AtsVendor.LEVER,
    version=VERSION,
    hosts=frozenset({"jobs.lever.co", "lever.co"}),
)

_SUBMIT_LABELS = frozenset({"submit application", "apply now"})


def _is_submit(element: PageElement) -> bool:
    return (
        element.role is ElementRole.BUTTON
        and bool(element.label)
        and element.label.lower() in _SUBMIT_LABELS
    )


class LeverDriver:
    @property
    def metadata(self) -> DriverMetadata:
        return METADATA

    def detects(self, url: str) -> Detection:
        detection = detect_ats(url)
        if detection.ats is AtsVendor.LEVER:
            return detection
        return Detection(AtsVendor.UNKNOWN, confidence=0.0, host=detection.host)

    async def run(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> DriverOutcome:
        url = context.job.apply_url or context.job.canonical_url
        if not url.rstrip("/").endswith("/apply"):
            url = url.rstrip("/") + "/apply"
        return await run_form_application(
            attempt,
            session,
            context,
            slug=SLUG,
            version=VERSION,
            started_message="lever application opened",
            apply_url=url,
            is_submit=_is_submit,
        )


def _create() -> LeverDriver:
    return LeverDriver()


PLUGIN = application_driver(
    slug=SLUG,
    name=METADATA.name,
    factory=_create,
    ats=AtsVendor.LEVER,
    description="Lever application workflow. Detection is from the apply URL.",
    priority=50,
)

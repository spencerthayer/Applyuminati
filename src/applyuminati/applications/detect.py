"""ATS detection from the application URL.

The discovery source is not consulted. LinkedIn, Indeed and a Greenhouse board
can all resolve to the same Workday posting; the apply URL decides the driver.
"""

from __future__ import annotations

from urllib.parse import urlparse

from applyuminati.core.models.job import AtsVendor, Job, canonicalize_url

__all__ = [
    "ATS_HOST_HINTS",
    "Detection",
    "detect_ats",
    "detect_job",
]


class Detection:
    """Result of inspecting an application URL."""

    __slots__ = ("ats", "confidence", "host")

    def __init__(self, ats: AtsVendor, *, confidence: float, host: str) -> None:
        self.ats = ats
        self.confidence = confidence
        self.host = host


#: Host suffixes that identify an ATS. Longest-match wins so
#: ``job-boards.greenhouse.io`` is Greenhouse, not a generic ``io``.
ATS_HOST_HINTS: tuple[tuple[str, AtsVendor], ...] = (
    ("boards.greenhouse.io", AtsVendor.GREENHOUSE),
    ("job-boards.greenhouse.io", AtsVendor.GREENHOUSE),
    ("greenhouse.io", AtsVendor.GREENHOUSE),
    ("jobs.lever.co", AtsVendor.LEVER),
    ("lever.co", AtsVendor.LEVER),
    ("myworkdayjobs.com", AtsVendor.WORKDAY),
    ("workday.com", AtsVendor.WORKDAY),
    ("jobs.ashbyhq.com", AtsVendor.ASHBY),
    ("ashbyhq.com", AtsVendor.ASHBY),
    ("jobs.smartrecruiters.com", AtsVendor.SMARTRECRUITERS),
    ("smartrecruiters.com", AtsVendor.SMARTRECRUITERS),
    ("icims.com", AtsVendor.ICIMS),
    ("taleo.net", AtsVendor.TALEO),
    ("successfactors.com", AtsVendor.SUCCESSFACTORS),
    ("jobvite.com", AtsVendor.JOBVITE),
    ("bamboohr.com", AtsVendor.BAMBOOHR),
    ("recruitee.com", AtsVendor.RECRUITEE),
    ("workable.com", AtsVendor.WORKABLE),
    ("teamtailor.com", AtsVendor.TEAMTAILOR),
    ("eightfold.ai", AtsVendor.EIGHTFOLD),
)


def detect_ats(url: str) -> Detection:
    """Identify the ATS from an application URL.

    Host matching only. Path heuristics belong in a driver that already knows
    it owns the host; putting them here would leak Greenhouse assumptions into
    every later ATS.
    """
    canonical = canonicalize_url(url)
    host = (urlparse(canonical).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    matches = [
        (suffix, vendor)
        for suffix, vendor in ATS_HOST_HINTS
        if host == suffix or host.endswith("." + suffix)
    ]
    if not matches:
        return Detection(AtsVendor.UNKNOWN, confidence=0.0, host=host)
    _suffix, vendor = max(matches, key=lambda item: len(item[0]))
    return Detection(vendor, confidence=1.0, host=host)


def detect_job(job: Job) -> Detection:
    """Detect from the job's apply URL, falling back to its canonical URL."""
    url = job.apply_url or job.canonical_url
    detection = detect_ats(url)
    if detection.ats is AtsVendor.UNKNOWN and job.ats not in (
        AtsVendor.UNKNOWN,
        AtsVendor.CUSTOM,
    ):
        # The job already carries an ATS from discovery metadata. Honour it
        # only when the URL told us nothing, so a LinkedIn-sourced Workday
        # posting is still Workday.
        return Detection(job.ats, confidence=0.5, host=detection.host)
    return detection

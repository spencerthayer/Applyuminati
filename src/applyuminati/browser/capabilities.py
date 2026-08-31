"""What a workflow needs from a browser, and whether a backend provides it.

Preference order alone is not a selection policy. "ego lite, else Playwright"
reads as sensible until ego lite is missing on a Linux server and an
application that needs the user's signed-in profile quietly proceeds in a
throwaway container, fills three pages, and dies at the login wall with a
half-finished attempt and no way back. The failure is silent and it is late.

So a workflow states its requirements up front:

    BrowserRequirements(
        required={PERSISTENT_LOGIN, HUMAN_HANDOFF, FILE_UPLOAD},
        preferred={AUTHENTICATED_USER_PROFILE, PERSISTENT_SESSION},
    )

and selection becomes a set operation. ``required`` is a hard filter: a backend
missing one of them is not a fallback, it is a wrong answer, and the error names
the capability rather than saying "browser error". ``preferred`` only breaks
ties, so asking for a nicety never causes a failure.

Where preference order still matters is *among* backends that all qualify.
Capabilities decide who is eligible; ``settings.browser.preferred`` decides who
wins. Capabilities are the veto, not the ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.browser.base import BrowserBackend, BrowserCapability, BrowserMetadata
from applyuminati.core.registry import HealthReport

__all__ = [
    "APPLICATION_SUBMISSION",
    "AUTHENTICATED_APPLICATION",
    "READ_ONLY_INSPECTION",
    "BackendCandidate",
    "BrowserRequirements",
    "CapabilityMaturity",
    "capability_matrix",
]


class CapabilityMaturity(StrEnum):
    """How far a capability claim has actually been exercised.

    A registered entry point and a capability tested end to end look identical
    in a feature table, and conflating them is how a README ends up promising
    something nobody has run. Backends may declare this per capability so
    documentation can be generated from code instead of from optimism.
    """

    #: The contract exists; nothing implements it yet.
    CONTRACT_ONLY = "contract_only"
    #: An adapter implements it, unverified against a real site.
    ADAPTER_EXISTS = "adapter_exists"
    #: A health probe confirms the backend is installed and responds.
    HEALTH_PROBE_WORKING = "health_probe_working"
    #: Used by a real workflow, covered by tests.
    WORKFLOW_INTEGRATED = "workflow_integrated"
    #: Exercised against real employer sites.
    PRODUCTION_TESTED = "production_tested"


class BrowserRequirements(BaseModel):
    """The capabilities a workflow needs, and the ones it merely wants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Absent any one of these, a backend is disqualified rather than degraded.
    required: frozenset[BrowserCapability] = Field(default_factory=frozenset)
    #: Tie-breakers among qualifying backends. Never a reason to fail.
    preferred: frozenset[BrowserCapability] = Field(default_factory=frozenset)
    #: Restricts selection to one backend slug, for "run this here" requests.
    backend_slug: str | None = None

    def missing_from(self, metadata: BrowserMetadata) -> frozenset[BrowserCapability]:
        """Required capabilities this backend does not advertise."""
        return frozenset(self.required - metadata.capabilities)

    def satisfied_by(self, metadata: BrowserMetadata) -> bool:
        return not self.missing_from(metadata)

    def preference_score(self, metadata: BrowserMetadata) -> int:
        """How many of the nice-to-haves this backend also covers."""
        return len(self.preferred & metadata.capabilities)

    def describe(self) -> str:
        """One line for an error message or a log field."""
        required = ", ".join(sorted(c.value for c in self.required)) or "none"
        preferred = ", ".join(sorted(c.value for c in self.preferred))
        return f"required: {required}" + (f"; preferred: {preferred}" if preferred else "")


#: Filling and submitting an application on a site the user must be signed into.
#: The common case, and the one that must never silently land on a backend that
#: cannot hand the login wall to a human.
AUTHENTICATED_APPLICATION = BrowserRequirements(
    required=frozenset(
        {
            BrowserCapability.NAVIGATE,
            BrowserCapability.SEMANTIC_SNAPSHOT,
            BrowserCapability.FILE_UPLOAD,
            BrowserCapability.PERSISTENT_LOGIN,
            BrowserCapability.HUMAN_HANDOFF,
        }
    ),
    preferred=frozenset(
        {
            BrowserCapability.AUTHENTICATED_USER_PROFILE,
            BrowserCapability.PERSISTENT_SESSION,
            BrowserCapability.MULTI_TAB,
            BrowserCapability.SCREENSHOT,
        }
    ),
)

#: Applying somewhere that takes a form and a resume without an account.
#: Handoff is still required: any site may present a challenge, and the answer
#: to a challenge is a human, never a workaround.
APPLICATION_SUBMISSION = BrowserRequirements(
    required=frozenset(
        {
            BrowserCapability.NAVIGATE,
            BrowserCapability.SEMANTIC_SNAPSHOT,
            BrowserCapability.FILE_UPLOAD,
            BrowserCapability.HUMAN_HANDOFF,
        }
    ),
    preferred=frozenset(
        {
            BrowserCapability.AUTHENTICATED_USER_PROFILE,
            BrowserCapability.PERSISTENT_SESSION,
            BrowserCapability.SCREENSHOT,
        }
    ),
)

#: Reading a posting. Deliberately undemanding, so job discovery is never
#: blocked by the absence of an interactive backend.
READ_ONLY_INSPECTION = BrowserRequirements(
    required=frozenset({BrowserCapability.NAVIGATE, BrowserCapability.SEMANTIC_SNAPSHOT}),
    preferred=frozenset({BrowserCapability.SCREENSHOT}),
)


@dataclass(frozen=True, slots=True)
class BackendCandidate:
    """One backend evaluated against a set of requirements.

    Kept for rejected candidates too: "ego_lite is not installed on linux" and
    "playwright cannot hand control to you" are the two sentences that make an
    unsatisfiable request diagnosable, and they are lost if only the winner is
    recorded.
    """

    slug: str
    #: None when the backend could not even be constructed.
    backend: BrowserBackend | None
    metadata: BrowserMetadata | None
    health: HealthReport | None
    missing: frozenset[BrowserCapability]
    preference_score: int
    #: Position in ``settings.browser.preferred``; lower wins.
    preference_rank: int
    rejection: str | None

    @property
    def eligible(self) -> bool:
        return self.rejection is None

    def describe(self) -> str:
        return f"{self.slug}: {self.rejection}" if self.rejection else f"{self.slug}: eligible"


def capability_matrix(
    metadata: list[BrowserMetadata],
) -> dict[str, dict[str, bool]]:
    """Backend slug to capability to supported, for generated documentation.

    Derived from the same metadata selection reads, so a capability table cannot
    drift from the code the way a hand-maintained one does.
    """
    return {
        entry.slug: {
            capability.value: capability in entry.capabilities for capability in BrowserCapability
        }
        for entry in sorted(metadata, key=lambda m: m.slug)
    }

"""Value objects shared across jobs, profiles and applications."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    CONTRACT_TO_HIRE = "contract_to_hire"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    APPRENTICESHIP = "apprenticeship"
    VOLUNTEER = "volunteer"
    UNKNOWN = "unknown"


class RemoteMode(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class SeniorityLevel(StrEnum):
    """Ordered ladder. :data:`SENIORITY_RANK` gives the numeric distance."""

    INTERN = "intern"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    VP = "vp"
    EXECUTIVE = "executive"
    UNKNOWN = "unknown"


SENIORITY_RANK: dict[SeniorityLevel, int] = {
    SeniorityLevel.INTERN: 0,
    SeniorityLevel.ENTRY: 1,
    SeniorityLevel.JUNIOR: 2,
    SeniorityLevel.MID: 3,
    SeniorityLevel.SENIOR: 4,
    SeniorityLevel.STAFF: 5,
    SeniorityLevel.LEAD: 5,
    SeniorityLevel.PRINCIPAL: 6,
    SeniorityLevel.MANAGER: 5,
    SeniorityLevel.DIRECTOR: 7,
    SeniorityLevel.VP: 8,
    SeniorityLevel.EXECUTIVE: 9,
}


def seniority_distance(left: SeniorityLevel, right: SeniorityLevel) -> int | None:
    """Ladder distance, or ``None`` when either side is unknown."""
    if left is SeniorityLevel.UNKNOWN or right is SeniorityLevel.UNKNOWN:
        return None
    return abs(SENIORITY_RANK[left] - SENIORITY_RANK[right])


class CompensationPeriod(StrEnum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


#: Rough annualisation factors. Used only for comparing a posting's range to a
#: user's requirement; never presented to the user as the employer's figure.
_ANNUALISATION: dict[CompensationPeriod, float] = {
    CompensationPeriod.HOURLY: 2080.0,
    CompensationPeriod.DAILY: 260.0,
    CompensationPeriod.WEEKLY: 52.0,
    CompensationPeriod.MONTHLY: 12.0,
    CompensationPeriod.YEARLY: 1.0,
}


class Compensation(BaseModel):
    """A pay range as stated by an employer, or required by a user.

    Currency conversion is deliberately not implemented: comparing across
    currencies without a rate source would silently invent numbers. Comparisons
    between different currencies return ``None`` (unknown) instead.
    """

    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None
    currency: str = "USD"
    period: CompensationPeriod = CompensationPeriod.YEARLY
    #: Employer stated an equity component; value unknown unless quantified.
    has_equity: bool | None = None
    bonus_percent: float | None = None
    #: Verbatim text the range was parsed from, kept for provenance.
    raw_text: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            object.__setattr__(self, "minimum", self.maximum)
            object.__setattr__(self, "maximum", self.minimum)
        return self

    @property
    def is_specified(self) -> bool:
        return self.minimum is not None or self.maximum is not None

    def annualised(self) -> tuple[float | None, float | None]:
        factor = _ANNUALISATION[self.period]
        low = self.minimum * factor if self.minimum is not None else None
        high = self.maximum * factor if self.maximum is not None else None
        return low, high

    def satisfies(self, requirement: Compensation) -> bool | None:
        """Does this range meet ``requirement``'s floor?

        ``None`` means "cannot tell" — unspecified range, or mismatched
        currency. Callers must treat ``None`` as an uncertainty, not a pass.
        """
        if not self.is_specified or not requirement.is_specified:
            return None
        if self.currency.upper() != requirement.currency.upper():
            return None
        _, offered_high = self.annualised()
        required_low, _ = requirement.annualised()
        if offered_high is None or required_low is None:
            return None
        return offered_high >= required_low


class Location(BaseModel):
    """A place, normalised as far as the source allows."""

    model_config = ConfigDict(extra="forbid")

    raw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    postal_code: str | None = None
    #: ISO-3166 alpha-2 when known; used for work-authorisation reasoning.
    country_code: str | None = None

    def display(self) -> str:
        parts = [p for p in (self.city, self.region, self.country) if p]
        return ", ".join(parts) or (self.raw or "Unspecified")

    @property
    def is_empty(self) -> bool:
        return not any((self.raw, self.city, self.region, self.country, self.postal_code))


class DateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str | None = None  # ISO-8601 date or year-month, as JSON Resume allows
    end: str | None = None
    is_current: bool = False


__all__ = [
    "SENIORITY_RANK",
    "Compensation",
    "CompensationPeriod",
    "DateRange",
    "EmploymentType",
    "Location",
    "RemoteMode",
    "SeniorityLevel",
    "seniority_distance",
]

"""The canonical career profile.

JSON Resume is the interchange format; this is the model Applyuminati
actually reasons over. It wraps a :class:`JsonResume` with everything the
standard has no place for: evidence, quantified metrics, STAR stories, wording
preferences, eligibility, questionnaire defaults and search targets.

The load-bearing idea is the **claim ledger**: every factual statement the
system may put in front of an employer lives in :attr:`CareerProfile.claims`
with an :class:`AssertionLevel` and provenance. Generated documents reference
claim ids; the fabrication guard rejects anything that does not.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.common import (
    Compensation,
    EmploymentType,
    Location,
    RemoteMode,
    SeniorityLevel,
)
from applyuminati.core.models.jsonresume import JsonResume
from applyuminati.core.provenance import AssertionLevel, Claim, Confidence
from applyuminati.core.strategy import SearchStrategy


class WorkAuthorizationStatus(StrEnum):
    CITIZEN = "citizen"
    PERMANENT_RESIDENT = "permanent_resident"
    WORK_VISA = "work_visa"
    STUDENT_VISA = "student_visa"
    WORKING_HOLIDAY = "working_holiday"
    REQUIRES_SPONSORSHIP = "requires_sponsorship"
    NOT_AUTHORIZED = "not_authorized"
    UNKNOWN = "unknown"


class ArtifactKind(StrEnum):
    REPOSITORY = "repository"
    LIVE_SITE = "live_site"
    ARTICLE = "article"
    TALK = "talk"
    DESIGN = "design"
    PAPER = "paper"
    PATENT = "patent"
    DATASET = "dataset"
    OTHER = "other"


class MetricUnit(StrEnum):
    PERCENT = "percent"
    CURRENCY = "currency"
    COUNT = "count"
    DURATION_SECONDS = "duration_seconds"
    RATIO = "ratio"
    OTHER = "other"


class QuantifiedMetric(BaseModel):
    """A number the user can defend in an interview.

    Metrics are separated from prose because tailoring is allowed to *restate*
    a metric and never allowed to invent or round one.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    label: str
    value: float
    unit: MetricUnit = MetricUnit.OTHER
    #: Free-text unit when :attr:`unit` is ``OTHER`` (e.g. "requests/second").
    unit_detail: str | None = None
    #: Employer or project this belongs to, matching a JSON Resume entry name.
    context: str | None = None
    period: str | None = None
    #: Claim that backs this metric. Metrics without a claim are not usable.
    claim_id: str | None = None

    def render(self) -> str:
        if self.unit is MetricUnit.PERCENT:
            return f"{self.value:g}%"
        if self.unit is MetricUnit.CURRENCY:
            return f"{self.value:,.0f}"
        suffix = f" {self.unit_detail}" if self.unit_detail else ""
        return f"{self.value:g}{suffix}"


class PortfolioArtifact(BaseModel):
    """A link to externally verifiable work."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: ArtifactKind = ArtifactKind.OTHER
    title: str
    url: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    #: Set once the user (or a fetch) confirmed the URL resolves.
    verified_at: datetime | None = None
    claim_ids: list[str] = Field(default_factory=list)


class StarStory(BaseModel):
    """A reusable interview story in Situation/Task/Action/Result form.

    ``reflection`` is kept because the strongest behavioural answers close on
    what the candidate would do differently, and that is hard to regenerate.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    title: str
    situation: str
    task: str
    action: str
    result: str
    reflection: str | None = None
    #: Competencies this story evidences: "conflict", "ambiguity", "scale"…
    themes: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    employer: str | None = None
    metric_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    #: Bumped when the user edits the story, so wording memory can diff it.
    revision: int = 1


class WordingPreference(BaseModel):
    """A phrase the user wants used, or never used again."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    phrase: str
    preferred: bool
    #: Where the preference applies: "resume", "cover_letter", "answers", "*".
    scope: str = "*"
    reason: str | None = None
    #: Incremented each time the signal is observed again; drives confidence.
    observations: int = 1
    created_at: datetime = Field(default_factory=utcnow)


class WritingStyle(BaseModel):
    """How the user writes, expressed as checkable constraints."""

    model_config = ConfigDict(extra="forbid")

    voice: str | None = None  # e.g. "first person, past tense, no pronouns"
    tone: str | None = None  # e.g. "direct, specific, no marketing language"
    max_bullet_words: int | None = 28
    prefer_active_voice: bool = True
    allow_first_person: bool = False
    banned_phrases: list[str] = Field(
        default_factory=lambda: [
            "results-driven",
            "synergy",
            "think outside the box",
            "passionate about",
            "rockstar",
            "ninja",
        ]
    )
    required_phrases: list[str] = Field(default_factory=list)
    notes: str | None = None


class JobTargets(BaseModel):
    """What the user is looking for."""

    model_config = ConfigDict(extra="forbid")

    titles: list[str] = Field(default_factory=list)
    #: Titles that superficially match but the user does not want. Used to
    #: *deprioritise*, never to silently drop a posting from the record.
    anti_titles: list[str] = Field(default_factory=list)
    seniority: SeniorityLevel = SeniorityLevel.UNKNOWN
    industries: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    remote_modes: list[RemoteMode] = Field(default_factory=lambda: [RemoteMode.REMOTE])
    employment_types: list[EmploymentType] = Field(
        default_factory=lambda: [EmploymentType.FULL_TIME]
    )
    #: The number below which the user will not proceed.
    compensation_floor: Compensation | None = None
    #: What the user is actually aiming for.
    compensation_target: Compensation | None = None
    #: Skills the user wants to keep using (positive signal in scoring).
    desired_skills: list[str] = Field(default_factory=list)
    #: Skills the user wants to stop using (negative signal, never a blocker).
    avoided_skills: list[str] = Field(default_factory=list)


class WorkEligibility(BaseModel):
    """Facts that can hard-block an application. Never inferred by a model."""

    model_config = ConfigDict(extra="forbid")

    #: Country code -> authorisation status in that country.
    authorization: dict[str, WorkAuthorizationStatus] = Field(default_factory=dict)
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool = False
    relocation_locations: list[Location] = Field(default_factory=list)
    #: ISO date, or "immediately".
    available_from: str | None = None
    notice_period_days: int | None = None
    max_travel_percent: int | None = None
    security_clearances: list[str] = Field(default_factory=list)

    def status_for(self, country_code: str | None) -> WorkAuthorizationStatus:
        if not country_code:
            return WorkAuthorizationStatus.UNKNOWN
        return self.authorization.get(country_code.upper(), WorkAuthorizationStatus.UNKNOWN)


class EEOPreferences(BaseModel):
    """Voluntary self-identification answers.

    Stored only when the user supplies them, defaulting to declining. These
    values are redacted from logs and never sent to an LLM.
    """

    model_config = ConfigDict(extra="forbid")

    #: When false (default) Applyuminati answers "decline to self-identify".
    disclose: bool = False
    gender: str | None = None
    race_ethnicity: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    notes: str | None = None


class QuestionnaireDefault(BaseModel):
    """A reusable answer to a recurring application question."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Normalised question key, e.g. ``years_of_experience_python``.
    key: str
    #: The question text as most recently seen, for matching and display.
    question_text: str | None = None
    answer: str
    level: AssertionLevel = AssertionLevel.PREFERENCE
    #: True for authorisation, salary, demographics, clearances, attestations.
    sensitive: bool = False
    claim_ids: list[str] = Field(default_factory=list)
    approvals: int = 0
    rejections: int = 0
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def confidence(self) -> Confidence:
        """Laplace-smoothed approval rate; drives whether we reuse the answer."""
        total = self.approvals + self.rejections
        return (self.approvals + 1) / (total + 2)


class CareerProfile(BaseModel):
    """The single canonical record of a user's career.

    There is exactly one active profile per installation in this release; the
    id is carried explicitly so multi-profile support is a data change rather
    than a schema change.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    label: str = "default"
    resume: JsonResume = Field(default_factory=JsonResume)

    claims: list[Claim] = Field(default_factory=list)
    metrics: list[QuantifiedMetric] = Field(default_factory=list)
    artifacts: list[PortfolioArtifact] = Field(default_factory=list)
    stories: list[StarStory] = Field(default_factory=list)

    wording_preferences: list[WordingPreference] = Field(default_factory=list)
    writing_style: WritingStyle = Field(default_factory=WritingStyle)

    targets: JobTargets = Field(default_factory=JobTargets)
    eligibility: WorkEligibility = Field(default_factory=WorkEligibility)
    eeo: EEOPreferences = Field(default_factory=EEOPreferences)
    questionnaire_defaults: list[QuestionnaireDefault] = Field(default_factory=list)
    strategy: SearchStrategy = Field(default_factory=SearchStrategy)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # -- lookups ----------------------------------------------------------

    def claim(self, claim_id: str) -> Claim | None:
        return next((c for c in self.claims if c.id == claim_id), None)

    def factual_claims(self) -> list[Claim]:
        """Claims that may be quoted as fact in generated material."""
        return [c for c in self.claims if c.is_factual]

    def claims_by_tag(self, tag: str) -> list[Claim]:
        lowered = tag.lower()
        return [c for c in self.claims if lowered in (t.lower() for t in c.tags)]

    def skill_names(self) -> set[str]:
        """Every skill token known from the resume and explicit targets."""
        names: set[str] = set()
        for skill in self.resume.skills:
            if skill.name:
                names.add(skill.name.strip().lower())
            names.update(k.strip().lower() for k in skill.keywords if k.strip())
        names.update(s.strip().lower() for s in self.targets.desired_skills if s.strip())
        return names

    def banned_phrases(self) -> set[str]:
        """Phrases the generator must not emit, from style plus learned rejections."""
        banned = {p.strip().lower() for p in self.writing_style.banned_phrases if p.strip()}
        banned.update(
            p.phrase.strip().lower() for p in self.wording_preferences if not p.preferred
        )
        return banned

    def default_answer(self, key: str) -> QuestionnaireDefault | None:
        return next((d for d in self.questionnaire_defaults if d.key == key), None)


__all__ = [
    "ArtifactKind",
    "CareerProfile",
    "EEOPreferences",
    "JobTargets",
    "MetricUnit",
    "PortfolioArtifact",
    "QuantifiedMetric",
    "QuestionnaireDefault",
    "StarStory",
    "WordingPreference",
    "WorkAuthorizationStatus",
    "WorkEligibility",
    "WritingStyle",
]

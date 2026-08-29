"""JSON Resume (https://jsonresume.org/) as typed Pydantic models.

This is the *interchange* format, not Applyuminati's internal model. It is
kept faithful to the published schema so that ``resume.json`` files round-trip
losslessly:

* every field is optional, because real-world resumes omit most of them;
* ``extra="allow"`` preserves ``x-``-prefixed vendor extensions;
* unknown keys survive export unchanged.

The richer canonical profile that wraps this lives in
:mod:`applyuminati.core.models.profile`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_CFG = ConfigDict(extra="allow", populate_by_name=True)


class ResumeLocation(BaseModel):
    model_config = _CFG

    address: str | None = None
    postalCode: str | None = None  # noqa: N815 - JSON Resume field name
    city: str | None = None
    countryCode: str | None = None  # noqa: N815
    region: str | None = None


class ResumeProfile(BaseModel):
    model_config = _CFG

    network: str | None = None
    username: str | None = None
    url: str | None = None


class ResumeBasics(BaseModel):
    model_config = _CFG

    name: str | None = None
    label: str | None = None
    image: str | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None
    summary: str | None = None
    location: ResumeLocation | None = None
    profiles: list[ResumeProfile] = Field(default_factory=list)


class ResumeWork(BaseModel):
    model_config = _CFG

    name: str | None = None
    position: str | None = None
    url: str | None = None
    startDate: str | None = None  # noqa: N815
    endDate: str | None = None  # noqa: N815
    summary: str | None = None
    highlights: list[str] = Field(default_factory=list)
    location: str | None = None
    description: str | None = None


class ResumeVolunteer(BaseModel):
    model_config = _CFG

    organization: str | None = None
    position: str | None = None
    url: str | None = None
    startDate: str | None = None  # noqa: N815
    endDate: str | None = None  # noqa: N815
    summary: str | None = None
    highlights: list[str] = Field(default_factory=list)


class ResumeEducation(BaseModel):
    model_config = _CFG

    institution: str | None = None
    url: str | None = None
    area: str | None = None
    studyType: str | None = None  # noqa: N815
    startDate: str | None = None  # noqa: N815
    endDate: str | None = None  # noqa: N815
    score: str | None = None
    courses: list[str] = Field(default_factory=list)


class ResumeAward(BaseModel):
    model_config = _CFG

    title: str | None = None
    date: str | None = None
    awarder: str | None = None
    summary: str | None = None


class ResumeCertificate(BaseModel):
    model_config = _CFG

    name: str | None = None
    date: str | None = None
    issuer: str | None = None
    url: str | None = None


class ResumePublication(BaseModel):
    model_config = _CFG

    name: str | None = None
    publisher: str | None = None
    releaseDate: str | None = None  # noqa: N815
    url: str | None = None
    summary: str | None = None


class ResumeSkill(BaseModel):
    model_config = _CFG

    name: str | None = None
    level: str | None = None
    keywords: list[str] = Field(default_factory=list)


class ResumeLanguage(BaseModel):
    model_config = _CFG

    language: str | None = None
    fluency: str | None = None


class ResumeInterest(BaseModel):
    model_config = _CFG

    name: str | None = None
    keywords: list[str] = Field(default_factory=list)


class ResumeReference(BaseModel):
    model_config = _CFG

    name: str | None = None
    reference: str | None = None


class ResumeProject(BaseModel):
    model_config = _CFG

    name: str | None = None
    description: str | None = None
    highlights: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    startDate: str | None = None  # noqa: N815
    endDate: str | None = None  # noqa: N815
    url: str | None = None
    roles: list[str] = Field(default_factory=list)
    entity: str | None = None
    type: str | None = None


class ResumeMeta(BaseModel):
    model_config = _CFG

    canonical: str | None = None
    version: str | None = None
    lastModified: str | None = None  # noqa: N815
    theme: str | None = None


class JsonResume(BaseModel):
    """A complete JSON Resume document."""

    model_config = _CFG

    schema_url: str | None = Field(default=None, alias="$schema")
    basics: ResumeBasics = Field(default_factory=ResumeBasics)
    work: list[ResumeWork] = Field(default_factory=list)
    volunteer: list[ResumeVolunteer] = Field(default_factory=list)
    education: list[ResumeEducation] = Field(default_factory=list)
    awards: list[ResumeAward] = Field(default_factory=list)
    certificates: list[ResumeCertificate] = Field(default_factory=list)
    publications: list[ResumePublication] = Field(default_factory=list)
    skills: list[ResumeSkill] = Field(default_factory=list)
    languages: list[ResumeLanguage] = Field(default_factory=list)
    interests: list[ResumeInterest] = Field(default_factory=list)
    references: list[ResumeReference] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)
    meta: ResumeMeta = Field(default_factory=ResumeMeta)

    def to_json_dict(self) -> dict[str, object]:
        """Export in JSON Resume shape: aliases restored, empty values dropped."""
        payload = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {key: value for key, value in payload.items() if value not in ([], {})}


__all__ = [
    "JsonResume",
    "ResumeAward",
    "ResumeBasics",
    "ResumeCertificate",
    "ResumeEducation",
    "ResumeInterest",
    "ResumeLanguage",
    "ResumeLocation",
    "ResumeMeta",
    "ResumeProfile",
    "ResumeProject",
    "ResumePublication",
    "ResumeReference",
    "ResumeSkill",
    "ResumeVolunteer",
    "ResumeWork",
]

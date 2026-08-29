"""Concrete prompts with output schemas.

Every system message states: fabrication is a failure, only supplied evidence
may be used, and returning "unknown" or null is always acceptable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from applyuminati.llm.prompts.base import PROMPT_REGISTRY, PromptTemplate, get_prompt, register

__all__ = ["PROMPT_REGISTRY", "PromptTemplate", "get_prompt", "register"]

_SYSTEM = (
    "You are part of Applyuminati, a local-first job search assistant. "
    "Fabrication is a failure: never invent employers, titles, dates, "
    "technologies, metrics or achievements that are not in the supplied evidence. "
    "If you cannot answer from the supplied information, return null or state "
    "that you need more information. Returning 'unknown' is always acceptable."
)


class DimensionAdjustment(BaseModel):
    dimension: str
    delta: float
    rationale: str = ""


class MissingRequirementSuggestion(BaseModel):
    requirement: str
    severity: str = "significant"
    note: str | None = None


class ScoreEnrichment(BaseModel):
    adjustments: list[DimensionAdjustment] = Field(default_factory=list)
    missing_requirements: list[MissingRequirementSuggestion] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    explanation: str = ""


class TailoredBullet(BaseModel):
    claim_id: str
    text: str


class TailoredBullets(BaseModel):
    bullets: list[TailoredBullet] = Field(default_factory=list)


class QuestionAnswer(BaseModel):
    answer: str | None = None
    confidence: float = 0.0
    evidence_claim_ids: list[str] = Field(default_factory=list)
    needs_user: bool = True
    rationale: str = ""


class EmailClassification(BaseModel):
    email_class: str
    confidence: float = 0.0
    extracted_dates: list[str] = Field(default_factory=list)
    extracted_links: list[str] = Field(default_factory=list)
    suggested_state: str | None = None
    rationale: str = ""


class Finding(BaseModel):
    topic: str
    statement: str
    confidence: float = 0.0
    source_url: str | None = None


class CompanyFindings(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


# -- Score enrichment ----------------------------------------------------
register(
    PromptTemplate(
        id="score.enrich",
        version="2026-01",
        description="Adjust deterministic dimension scores within bounded limits.",
        output_schema=ScoreEnrichment,
        system=_SYSTEM + " You may ONLY adjust existing dimension scores by at most ±0.2, add "
        "uncertainties, add missing requirements, and rewrite the explanation. "
        "You may NOT produce an overall score or a recommendation.",
        template=(
            "Job: $job_title at $company\n"
            "Description excerpt: $description_excerpt\n\n"
            "Candidate profile summary: $profile_summary\n\n"
            "Deterministic dimension scores:\n$dimensions\n\n"
            "Return bounded adjustments as JSON."
        ),
    )
)

# -- Resume tailoring -----------------------------------------------------
register(
    PromptTemplate(
        id="resume.tailor",
        version="2026-01",
        description="Rewrite existing verified claims more clearly for a job.",
        output_schema=TailoredBullets,
        system=_SYSTEM + " You may ONLY rewrite existing verified claims more clearly. "
        "You may NOT add employers, titles, dates, technologies, metrics or "
        "achievements not present in the supplied claims.",
        template=(
            "Job: $job_title at $company\n"
            "Description excerpt: $description_excerpt\n\n"
            "Verified claims to rewrite (keep claim_id unchanged):\n$bullets\n\n"
            "Return rewritten bullets as JSON."
        ),
    )
)

# -- Questionnaire answering ----------------------------------------------
register(
    PromptTemplate(
        id="questionnaire.answer",
        version="2026-01",
        description="Answer an employer question from supplied evidence.",
        output_schema=QuestionAnswer,
        system=_SYSTEM + " Sensitive questions (work authorisation, salary, demographics, "
        "clearances, legal attestations) must NEVER be answered by inference. "
        "When evidence is insufficient, needs_user=true is the CORRECT answer.",
        template=(
            "Question: $question_text\n"
            "Job context: $job_context\n\n"
            "Available evidence:\n$evidence\n\n"
            "Return an answer as JSON."
        ),
    )
)

# -- Email classification -------------------------------------------------
# Kept in sync by hand with applyuminati.email.base.EmailClass's values.
# Not imported directly: `llm` and `email` are independent siblings in the
# layered architecture, and this prompt module must load with no email
# dependency at all.
_EMAIL_CLASSES = (
    "application_confirmation",
    "recruiter_outreach",
    "rejection",
    "assessment_request",
    "interview_request",
    "scheduling",
    "offer",
    "information_request",
    "marketing",
    "unrelated",
)
_email_classes = ", ".join(_EMAIL_CLASSES)
register(
    PromptTemplate(
        id="email.classify",
        version="2026-01",
        description="Classify an employer email and extract dates/links.",
        output_schema=EmailClassification,
        system=_SYSTEM + f" The email_class must be one of: {_email_classes}.",
        template=(
            "Subject: $subject\nFrom: $sender\nBody:\n$body\n\nReturn a classification as JSON."
        ),
    )
)

# -- Company research -----------------------------------------------------
register(
    PromptTemplate(
        id="research.company",
        version="2026-01",
        description="Summarise supplied source text into topic-tagged findings.",
        output_schema=CompanyFindings,
        system=_SYSTEM + " You may ONLY use the supplied source text. Do NOT answer from "
        "your parametric memory. Each finding must cite its source_url.",
        template=(
            "Company: $company\nSource text:\n$source_text\n\nReturn topic-tagged findings as JSON."
        ),
    )
)

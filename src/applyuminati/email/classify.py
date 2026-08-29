"""Deterministic email classifier.

Keyword and sender-domain rules, no LLM required. The LLM may later refine
these labels, but reading the mailbox must never depend on one being
configured — a deterministic first pass is the honest floor.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime

from applyuminati.email.base import EmailClass, EmailMessage, MessageClassification

__all__ = ["classify_message"]


_REJECTION_KEYWORDS = (
    "regret",
    "unfortunately",
    "not moving forward",
    "not to proceed",
    "decided not to",
    "other candidates",
    "position has been filled",
    "no longer under consideration",
    "we won't be",
)
_INTERVIEW_KEYWORDS = (
    "interview",
    "schedule a call",
    "phone screen",
    "tech screen",
    "onsite",
    "virtual interview",
    "invite you to",
)
_ASSESSMENT_KEYWORDS = (
    "assessment",
    "coding challenge",
    "take-home",
    "hackerrank",
    "codility",
    "complete the following",
)
_OFFER_KEYWORDS = ("offer", "congratulations", "pleased to offer", "compensation package")
_RECRUITER_KEYWORDS = (
    "reached out",
    "opportunity that matches",
    "your profile",
    "wanted to connect",
    "recruiter",
    "talent acquisition",
)
_CONFIRMATION_KEYWORDS = (
    "received your application",
    "thank you for applying",
    "application has been",
    "we have received",
    "successfully submitted",
)


def _extract_dates(text: str) -> list[datetime]:
    dates: list[datetime] = []
    # ISO dates: 2026-01-15
    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        with contextlib.suppress(ValueError):
            dates.append(
                datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=UTC)
            )
    # Written dates: January 15, 2026 / Jan 15 2026
    for match in re.finditer(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    ):
        month_map = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        with contextlib.suppress(ValueError, KeyError):
            dates.append(
                datetime(
                    int(match.group(3)),
                    month_map[match.group(1).lower()],
                    int(match.group(2)),
                    tzinfo=UTC,
                )
            )
    return dates


def _extract_links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>\"]+", text)


_STATE_MAP: dict[EmailClass, str] = {
    EmailClass.APPLICATION_CONFIRMATION: "submitted",
    EmailClass.RECRUITER_OUTREACH: "recruiter_contact",
    EmailClass.REJECTION: "rejected",
    EmailClass.ASSESSMENT_REQUEST: "assessment",
    EmailClass.INTERVIEW_REQUEST: "interview",
    EmailClass.OFFER: "offer",
}


def classify_message(message: EmailMessage) -> MessageClassification:
    """Classify a message deterministically by sender domain and keywords."""
    combined = f"{message.subject} {message.body_text or message.snippet or ''}".lower()
    sender_domain = message.sender.domain

    # Priority: offer > interview > assessment > rejection > confirmation > recruiter
    if any(kw in combined for kw in _OFFER_KEYWORDS):
        email_class = EmailClass.OFFER
        confidence = 0.85
    elif any(kw in combined for kw in _INTERVIEW_KEYWORDS):
        email_class = EmailClass.INTERVIEW_REQUEST
        confidence = 0.8
    elif any(kw in combined for kw in _ASSESSMENT_KEYWORDS):
        email_class = EmailClass.ASSESSMENT_REQUEST
        confidence = 0.75
    elif any(kw in combined for kw in _REJECTION_KEYWORDS):
        email_class = EmailClass.REJECTION
        confidence = 0.8
    elif any(kw in combined for kw in _CONFIRMATION_KEYWORDS):
        email_class = EmailClass.APPLICATION_CONFIRMATION
        confidence = 0.7
    elif any(kw in combined for kw in _RECRUITER_KEYWORDS) or any(
        domain in sender_domain for domain in ("linkedin.com", "wellfound.com", "hired.com")
    ):
        email_class = EmailClass.RECRUITER_OUTREACH
        confidence = 0.6
    else:
        email_class = EmailClass.UNRELATED
        confidence = 0.4

    full_text = f"{message.subject} {message.body_text or ''}"
    dates = _extract_dates(full_text)
    links = _extract_links(full_text)
    suggested = _STATE_MAP.get(email_class)

    return MessageClassification(
        message_id=message.id,
        email_class=email_class,
        confidence=confidence,
        suggested_state=suggested,
        extracted_dates=dates,
        extracted_links=links,
        rationale=f"keyword match for {email_class.value}",
    )

"""Rendering as a separate concern.

Tailoring produces a :class:`JsonResume`; rendering turns it into a deliverable
format. Keeping the two apart means a new template or format (PDF, DOCX, LaTeX)
is an adapter here, not a change to the tailoring logic.
"""

from __future__ import annotations

from typing import Protocol

from applyuminati.core.models.jsonresume import JsonResume

__all__ = ["RENDERER_REGISTRY", "JsonRenderer", "MarkdownRenderer", "ResumeRenderer"]


class ResumeRenderer(Protocol):
    format: str

    def render(self, resume: JsonResume) -> bytes: ...


class JsonRenderer:
    format = "json"

    def render(self, resume: JsonResume) -> bytes:
        import json

        return json.dumps(resume.to_json_dict(), indent=2, ensure_ascii=False).encode("utf-8")


class MarkdownRenderer:
    """A plain-text resume no one would call beautiful, but that is readable."""

    format = "markdown"

    def render(self, resume: JsonResume) -> bytes:
        lines: list[str] = []
        basics = resume.basics
        if basics.name:
            lines.append(f"# {basics.name}")
        if basics.label:
            lines.append(f"**{basics.label}**")
        if basics.email or basics.url:
            lines.append(" | ".join(filter(None, [basics.email, basics.url])))
        if basics.summary:
            lines.append("")
            lines.append(basics.summary)
        for work in resume.work:
            lines.append("")
            lines.append(f"## {work.position or ''} — {work.name or ''}")
            dates = " - ".join(filter(None, [work.startDate, work.endDate]))
            if dates:
                lines.append(f"*{dates}*")
            if work.summary:
                lines.append(work.summary)
            for highlight in work.highlights:
                lines.append(f"- {highlight}")
        for edu in resume.education:
            lines.append("")
            lines.append(f"## {edu.institution or ''}")
            lines.append(f"{edu.studyType or ''}, {edu.area or ''}")
        lines.append("")
        return "\n".join(lines).encode("utf-8")


RENDERER_REGISTRY: dict[str, ResumeRenderer] = {
    "json": JsonRenderer(),
    "markdown": MarkdownRenderer(),
}

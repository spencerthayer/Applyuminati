"""Resume: import, export, evidence, fabrication guard, tailoring, rendering.

The guard is the load-bearing piece: it is what makes "tailoring may not
invent" a programmatic check rather than a prompt instruction. Every tailored
resume is passed through it before it reaches the user or an application.
"""

from applyuminati.resume.exporter import export_json_resume
from applyuminati.resume.guard import FabricationGuard, GuardReport, GuardSeverity, GuardViolation
from applyuminati.resume.importer import import_json_resume
from applyuminati.resume.render import (
    JsonRenderer,
    MarkdownRenderer,
    RENDERER_REGISTRY,
    ResumeRenderer,
)
from applyuminati.resume.tailor import ResumeTailor, TailorResult
from applyuminati.resume.evidence import EvidenceIndex

__all__ = [
    "EvidenceIndex",
    "FabricationGuard",
    "GuardReport",
    "GuardSeverity",
    "GuardViolation",
    "JsonRenderer",
    "MarkdownRenderer",
    "RENDERER_REGISTRY",
    "ResumeRenderer",
    "ResumeTailor",
    "TailorResult",
    "export_json_resume",
    "import_json_resume",
]

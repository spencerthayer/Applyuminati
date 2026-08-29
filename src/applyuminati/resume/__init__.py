"""Resume: import, export, evidence, fabrication guard, tailoring, rendering.

The guard is the load-bearing piece: it is what makes "tailoring may not
invent" a programmatic check rather than a prompt instruction. Every tailored
resume is passed through it before it reaches the user or an application.
"""

from applyuminati.resume.evidence import EvidenceIndex
from applyuminati.resume.exporter import export_json_resume
from applyuminati.resume.guard import FabricationGuard, GuardReport, GuardSeverity, GuardViolation
from applyuminati.resume.importer import import_json_resume
from applyuminati.resume.render import (
    RENDERER_REGISTRY,
    JsonRenderer,
    MarkdownRenderer,
    ResumeRenderer,
)
from applyuminati.resume.tailor import ResumeTailor, TailorResult

__all__ = [
    "RENDERER_REGISTRY",
    "EvidenceIndex",
    "FabricationGuard",
    "GuardReport",
    "GuardSeverity",
    "GuardViolation",
    "JsonRenderer",
    "MarkdownRenderer",
    "ResumeRenderer",
    "ResumeTailor",
    "TailorResult",
    "export_json_resume",
    "import_json_resume",
]

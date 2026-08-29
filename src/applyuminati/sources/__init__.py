"""Job-source contract and shared normalisation.

Re-exports the contract types so a consumer can ``from applyuminati.sources
import JobSource, DiscoveryRequest`` without reaching into ``sources.base``.
"""

from applyuminati.sources.base import (
    SOURCE_REGISTRY,
    BlockingBehavior,
    DiscoveryRequest,
    FreshnessResult,
    JobSource,
    RateLimit,
    SourceCapability,
    SourceFailure,
    SourceMetadata,
    SourceResult,
    source_plugin,
)
from applyuminati.sources.dedup import Deduplicator, similarity
from applyuminati.sources.normalize import build_job, build_source_record, parse_compensation
from applyuminati.sources.text import (
    TECH_VOCABULARY,
    extract_skills,
    html_to_text,
    split_requirements,
)

__all__ = [
    "SOURCE_REGISTRY",
    "TECH_VOCABULARY",
    "BlockingBehavior",
    "Deduplicator",
    "DiscoveryRequest",
    "FreshnessResult",
    "JobSource",
    "RateLimit",
    "SourceCapability",
    "SourceFailure",
    "SourceMetadata",
    "SourceResult",
    "build_job",
    "build_source_record",
    "extract_skills",
    "html_to_text",
    "parse_compensation",
    "similarity",
    "source_plugin",
    "split_requirements",
]

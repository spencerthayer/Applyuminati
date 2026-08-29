"""Memory: explicit categories, learning signals, and retrieval.

The store enforces the hard rule: a machine can never write at VERIFIED or
USER_APPROVED without explicit user approval. Learning signals from edits
become writing-memory at PREFERENCE level, never canonical fact overwrites.
"""

from applyuminati.memory.learning import (
    apply_signal,
    diff_signal,
    record_approval,
    record_outcome,
)
from applyuminati.memory.retrieval import MemoryBundle, MemoryRetriever
from applyuminati.memory.store import MemoryStore

__all__ = [
    "MemoryBundle",
    "MemoryRetriever",
    "MemoryStore",
    "apply_signal",
    "diff_signal",
    "record_approval",
    "record_outcome",
]

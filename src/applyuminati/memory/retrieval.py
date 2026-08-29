"""Memory retrieval: assemble a bounded grounding bundle for generation.

Keyword and recency based, not vector based. This is the seam where a vector
store would later plug in: replace the ranking with an embedding similarity
query and the rest of the pipeline stays the same. We are not adding a vector
dependency until retrieval quality actually demands it — premature
infrastructure is an anti-pattern this project explicitly rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from applyuminati.core.models.memory import MemoryKind, MemoryRecord
from applyuminati.memory.store import MemoryStore

__all__ = ["MemoryBundle", "MemoryRetriever"]


@dataclass(frozen=True, slots=True)
class MemoryBundle:
    """A bounded, prioritised collection of memory for a generation call."""

    records: list[MemoryRecord] = field(default_factory=list)
    total_available: int = 0
    dropped: int = 0
    chars: int = 0


class MemoryRetriever:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def bundle(
        self,
        *,
        kinds: list[MemoryKind],
        scope: str | None = None,
        budget_chars: int = 4000,
        min_confidence: float = 0.3,
    ) -> MemoryBundle:
        """Assemble a grounding bundle within a character budget."""
        all_records: list[MemoryRecord] = []
        for kind in kinds:
            records = await self._store.recall(
                kind, scope=scope, min_confidence=min_confidence, limit=50
            )
            all_records.extend(records)

        # Rank by confidence x recency (newer updated_at wins ties).
        all_records.sort(key=lambda r: (r.confidence, r.updated_at), reverse=True)

        # Deduplicate by (kind, scope, key).
        seen: set[str] = set()
        unique: list[MemoryRecord] = []
        for record in all_records:
            dedup_key = f"{record.kind.value}:{record.scope}:{record.key}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            unique.append(record)

        # Truncate to the character budget.
        total_chars = 0
        kept: list[MemoryRecord] = []
        for record in unique:
            record_chars = len(record.content)
            if total_chars + record_chars > budget_chars:
                break
            total_chars += record_chars
            kept.append(record)

        return MemoryBundle(
            records=kept,
            total_available=len(unique),
            dropped=len(unique) - len(kept),
            chars=total_chars,
        )

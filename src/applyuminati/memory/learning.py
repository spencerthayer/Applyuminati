"""Learning from user edits and outcomes.

When a user edits generated material, the difference is a training signal.
``diff_signal`` classifies the edit using ``difflib.SequenceMatcher`` and
``apply_signal`` turns it into writing-memory records: removed phrases become
rejected wording preferences, added phrases become preferred ones, and a
numeric change records a fact-correction memory at ``INFERRED`` that
explicitly does NOT overwrite the canonical claim.

Outcomes are recorded without asserting causation — a rejection after using
a phrasing is correlation, and the derived memory says so.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from applyuminati.core.ids import new_ulid
from applyuminati.core.models.memory import (
    ApprovalSignal,
    EditKind,
    LearningSignal,
    MemoryKind,
    MemoryRecord,
    OutcomeRecord,
)
from applyuminati.core.provenance import AssertionLevel, Provenance, ProvenanceKind
from applyuminati.memory.store import MemoryStore

__all__ = ["apply_signal", "diff_signal", "record_approval", "record_outcome"]


def diff_signal(generated: str, user_text: str, **context: object) -> LearningSignal:
    """Classify the difference between generated and user-edited text."""
    edit_kinds: list[EditKind] = []

    if generated.strip() and not user_text.strip():
        edit_kinds.append(EditKind.OMISSION)
    elif not generated.strip() and user_text.strip():
        edit_kinds.append(EditKind.INCLUSION)

    if generated.strip() and user_text.strip():
        ratio = SequenceMatcher(None, generated, user_text).ratio()
        if ratio < 0.3:
            edit_kinds.append(EditKind.WORDING)
        if len(user_text) < len(generated) * 0.6 or len(user_text) > len(generated) * 1.4:
            edit_kinds.append(EditKind.LENGTH)

        # Detect numeric changes → fact correction.
        import re

        gen_nums = set(re.findall(r"\d[\d,]*\.?\d+", generated))
        user_nums = set(re.findall(r"\d[\d,]*\.?\d+", user_text))
        if gen_nums != user_nums and gen_nums and user_nums:
            edit_kinds.append(EditKind.FACT_CORRECTION)

        # Detect reordering: same tokens, different order.
        gen_tokens = generated.split()
        user_tokens = user_text.split()
        if sorted(gen_tokens) == sorted(user_tokens) and gen_tokens != user_tokens:
            edit_kinds.append(EditKind.ORDERING)

        # Tone markers.
        tone_markers = ["however", "notably", "importantly"]
        if any(marker in user_text.lower() for marker in tone_markers) and not any(
            marker in generated.lower() for marker in tone_markers
        ):
            edit_kinds.append(EditKind.TONE)

    if not edit_kinds:
        edit_kinds.append(EditKind.WORDING)

    return LearningSignal(
        id=new_ulid(),
        artifact_kind=str(context.get("artifact_kind", "resume")),
        artifact_id=str(context["artifact_id"]) if "artifact_id" in context else None,
        target_path=str(context["target_path"]) if "target_path" in context else None,
        generated_text=generated,
        user_text=user_text,
        edit_kinds=edit_kinds,
        job_id=str(context["job_id"]) if "job_id" in context else None,
        application_id=str(context["application_id"]) if "application_id" in context else None,
        prompt_version=str(context["prompt_version"]) if "prompt_version" in context else None,
        llm_model=str(context["llm_model"]) if "llm_model" in context else None,
    )


async def apply_signal(signal: LearningSignal, store: MemoryStore) -> list[MemoryRecord]:
    """Turn an edit signal into writing-memory records."""
    records: list[MemoryRecord] = []
    scope = f"artifact:{signal.artifact_kind}"

    if EditKind.OMISSION in signal.edit_kinds:
        # The user deleted our text → record it as a rejected phrase.
        for phrase in _extract_phrases(signal.generated_text):
            record = await store.remember(
                MemoryKind.WRITING,
                scope=scope,
                key=f"rejected:{phrase[:80]}",
                content=f"User removed phrase: {phrase!r}",
                level=AssertionLevel.PREFERENCE,
                provenance=[
                    Provenance(
                        kind=ProvenanceKind.LLM,
                        origin=signal.llm_model or "unknown",
                        locator=signal.target_path,
                    )
                ],
            )
            records.append(record)

    if EditKind.INCLUSION in signal.edit_kinds or EditKind.WORDING in signal.edit_kinds:
        for phrase in _extract_phrases(signal.user_text):
            if phrase not in signal.generated_text:
                record = await store.remember(
                    MemoryKind.WRITING,
                    scope=scope,
                    key=f"preferred:{phrase[:80]}",
                    content=f"User introduced phrase: {phrase!r}",
                    level=AssertionLevel.PREFERENCE,
                    provenance=[
                        Provenance(
                            kind=ProvenanceKind.USER_INPUT,
                            origin="edit",
                            locator=signal.target_path,
                        )
                    ],
                )
                records.append(record)

    if EditKind.FACT_CORRECTION in signal.edit_kinds:
        record = await store.remember(
            MemoryKind.FACTUAL_CAREER,
            scope=scope,
            key=f"fact_correction:{signal.target_path or 'unknown'}",
            content=(
                f"User corrected a numeric value. "
                f"Generated: {signal.generated_text[:200]!r}, "
                f"User: {signal.user_text[:200]!r}. "
                f"This is a correction signal, NOT a canonical fact update."
            ),
            level=AssertionLevel.INFERRED,
            provenance=[
                Provenance(
                    kind=ProvenanceKind.USER_INPUT,
                    origin="edit",
                    locator=signal.target_path,
                )
            ],
        )
        records.append(record)

    return records


def _extract_phrases(text: str) -> list[str]:
    """Extract meaningful phrases from text for wording memory."""
    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if len(s.strip()) > 10]
    return sentences[:5]


async def record_approval(signal: ApprovalSignal, store: MemoryStore) -> MemoryRecord:
    """Record a user's explicit approval or rejection of something."""
    return await store.remember(
        MemoryKind.USER_PREFERENCE,
        scope=f"subject:{signal.subject_kind}",
        key=signal.subject_key,
        content=(
            f"User {'approved' if signal.approved else 'rejected'} "
            f"{signal.subject_kind}: {signal.subject_key}"
        ),
        level=AssertionLevel.PREFERENCE,
        data={"approved": signal.approved, "reason": signal.reason, "job_id": signal.job_id},
        provenance=[
            Provenance(
                kind=ProvenanceKind.USER_INPUT,
                origin="approval",
            )
        ],
    )


async def record_outcome(outcome: OutcomeRecord, store: MemoryStore) -> MemoryRecord:
    """Record an application outcome as correlational memory.

    The outcome record explicitly sets ``causation_known=False``. The derived
    memory phrases itself correlationally: "application using X resulted in Y"
    — never "X caused Y".
    """
    return await store.remember(
        MemoryKind.OUTCOME,
        scope=f"application:{outcome.application_id}",
        key=f"outcome:{outcome.outcome}",
        content=(
            f"Application to {outcome.source or 'unknown source'} "
            f"via {outcome.ats or 'unknown ATS'} "
            f"with fit score {outcome.fit_score} resulted in {outcome.outcome} "
            f"after {outcome.days_to_outcome:.1f} days. "
            f"Causation is NOT known."
        ),
        level=AssertionLevel.INFERRED,
        data={
            "outcome": outcome.outcome,
            "fit_score": outcome.fit_score,
            "ats": outcome.ats,
            "source": outcome.source,
            "causation_known": False,
        },
        provenance=[
            Provenance(
                kind=ProvenanceKind.SYSTEM,
                origin="outcome_tracker",
            )
        ],
    )

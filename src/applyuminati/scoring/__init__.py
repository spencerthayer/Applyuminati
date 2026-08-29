"""Scoring: deterministic engine plus optional LLM enrichment.

The deterministic engine is the floor — it always runs, needs no provider,
and its result is fully inspectable. The LLM pass is unprivileged enrichment
on top, clamped and recomputed by the engine so a model can never move a
score without justification or override a hard blocker.
"""

from applyuminati.scoring.engine import SCORER_VERSION, score_job
from applyuminati.scoring.llm_pass import enrich_score

__all__ = ["SCORER_VERSION", "enrich_score", "score_job"]

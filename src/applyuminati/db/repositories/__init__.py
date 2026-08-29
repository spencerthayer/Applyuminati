"""Repositories: the only sanctioned way to read or write domain objects.

Each repository is bound to one ``AsyncSession``; a transaction boundary is a
unit of work in the services layer, never inside a repository method.
"""

from applyuminati.db.repositories.applications import ApplicationRepository
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.db.repositories.llm_calls import LLMCallRepository
from applyuminati.db.repositories.memory import MemoryRepository
from applyuminati.db.repositories.profiles import ProfileRepository
from applyuminati.db.repositories.research import ResearchRepository
from applyuminati.db.repositories.runs import RunRepository
from applyuminati.db.repositories.scores import ScoreRepository
from applyuminati.db.repositories.sources import SourceState, SourceStateRepository
from applyuminati.db.repositories.tasks import TaskRepository

__all__ = [
    "ApplicationRepository",
    "JobRepository",
    "LLMCallRepository",
    "MemoryRepository",
    "ProfileRepository",
    "ResearchRepository",
    "RunRepository",
    "ScoreRepository",
    "SourceState",
    "SourceStateRepository",
    "TaskRepository",
]

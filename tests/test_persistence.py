"""Tests for profile persistence, job upsert with dedup, and configuration validation."""

from __future__ import annotations

from applyuminati.core.models.job import SourceTier
from applyuminati.db.repositories import JobRepository, ProfileRepository
from applyuminati.resume.importer import import_json_resume
from applyuminati.sources.normalize import build_job


async def test_profile_round_trips(database, sample_resume) -> None:
    profile, _ = import_json_resume(sample_resume)
    async with database.session() as session:
        repo = ProfileRepository(session)
        saved = await repo.upsert(profile)
        loaded = await repo.get(saved.id)
    assert loaded is not None
    assert loaded.resume.basics.name == "Jane Engineer"
    assert len(loaded.claims) == len(profile.claims)


async def test_job_upsert_creates_then_merges(database) -> None:
    job1 = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Software Engineer",
        company="Acme",
    )
    job2 = build_job(
        source="local_feed",
        tier=SourceTier.DERIVED,
        source_job_id="2",
        url="https://example.com/jobs/2",
        title="Software Engineer",
        company="Acme",
    )
    async with database.session() as session:
        repo = JobRepository(session)
        _created, was_created = await repo.upsert(job1)
        assert was_created
        merged, was_created2 = await repo.upsert(job2)
        assert not was_created2
        assert len(merged.sources) == 2
        # Direct ATS title should win over derived.
        assert merged.title == "Software Engineer"


async def test_job_list_filters_by_score_absence(database) -> None:
    job = build_job(
        source="test",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url="https://example.com/1",
        title="Engineer",
        company="Acme",
    )
    async with database.session() as session:
        repo = JobRepository(session)
        await repo.upsert(job)
        _jobs_no_score, total_no = await repo.list(has_score=False)
        _jobs_with_score, total_with = await repo.list(has_score=True)
    assert total_no == 1
    assert total_with == 0


def test_configuration_validates_default_provider(tmp_path) -> None:
    import pytest

    from applyuminati.core.settings import LLMSettings, Settings

    with pytest.raises(ValueError, match="default_provider"):
        Settings(
            data_dir=tmp_path / "data",
            llm=LLMSettings(default_provider="nonexistent", providers={}),
        )


def test_strategy_validates_thresholds() -> None:
    import pytest

    from applyuminati.core.strategy import SearchStrategy

    with pytest.raises(ValueError, match="skip_below_score"):
        SearchStrategy(skip_below_score=0.8, minimum_fit_score=0.5)

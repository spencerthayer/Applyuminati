"""Tests for job normalization, deduplication, and source plugin behaviour."""

from __future__ import annotations

from applyuminati.core.models.common import Location, RemoteMode
from applyuminati.core.models.job import SourceTier
from applyuminati.sources.dedup import Deduplicator, similarity
from applyuminati.sources.normalize import build_job, parse_compensation
from applyuminati.sources.text import extract_skills, html_to_text, split_requirements


def test_html_to_text_strips_tags() -> None:
    html = "<p>Hello <b>world</b></p><script>bad()</script>"
    text = html_to_text(html)
    assert "Hello world" in text
    assert "bad()" not in text


def test_split_requirements() -> None:
    text = "Requirements:\n- Python\n- SQL\n\nNice to have:\n- Kafka\n- Spark"
    required, preferred = split_requirements(text)
    assert "Python" in required
    assert "SQL" in required
    assert "Kafka" in preferred
    assert "Spark" in preferred


def test_extract_skills() -> None:
    text = "We use Python, TypeScript, and Kafka."
    skills = extract_skills(text)
    assert "python" in skills
    assert "typescript" in skills
    assert "kafka" in skills


def test_parse_compensation_range() -> None:
    comp = parse_compensation("$120k - $150k/yr")
    assert comp is not None
    assert comp.minimum == 120_000
    assert comp.maximum == 150_000


def test_parse_compensation_hourly() -> None:
    comp = parse_compensation("$60/hr")
    assert comp is not None
    assert comp.minimum == 60


def test_parse_compensation_returns_none_for_ambiguous() -> None:
    assert parse_compensation("competitive salary") is None


def test_build_job_normalizes() -> None:
    job = build_job(
        source="test",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="123",
        url="https://example.com/jobs/123?utm_source=test",
        title="Senior Software Engineer",
        company="Acme",
        description="<p>We need <b>Python</b> engineers.</p>",
        locations=[Location(raw="Remote")],
    )
    assert job.company_key == "acme"
    assert "utm_source" not in job.canonical_url
    assert job.remote_mode == RemoteMode.REMOTE
    assert "Python" in job.skills or "python" in job.skills
    assert job.seniority.value == "senior"


def test_dedup_merges_same_job_from_two_sources() -> None:
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
    dedup = Deduplicator()
    assert dedup.is_duplicate(job1, job2)
    merged = dedup.merge(job1, job2)
    assert len(merged.sources) == 2
    assert merged.sources[0].source == "greenhouse"  # direct ATS wins


def test_similarity_near_miss_titles() -> None:
    job_a = build_job(
        source="a",
        tier=SourceTier.AGGREGATOR,
        source_job_id="1",
        url="https://a.com/1",
        title="Senior Software Engineer",
        company="Acme",
    )
    job_b = build_job(
        source="b",
        tier=SourceTier.AGGREGATOR,
        source_job_id="2",
        url="https://b.com/2",
        title="Sr. Software Engineer",
        company="Acme",
    )
    assert similarity(job_a, job_b) > 0.8

# Architecture

## Overview

Applyuminati is a local-first, autonomous, LLM-powered job search and application platform. The architecture is designed so that every extension point — job sources, LLM providers, browser backends, agent runtimes, email providers — can be added without changing unrelated modules.

## Layering

Dependency direction enforced by [import-linter](https://github.com/snrk/import-linter) in CI:

```
CLI | API | host  →  services  →  plugins  →  applications  →  other domain packages  →  db  →  core
```

The contract is the import-linter table in `pyproject.toml`. `host` sits with CLI and API because the native Browser Host companion is an entry surface, not a domain package. `applications` is its own layer so application drivers can depend on browser and questionnaire contracts without the reverse.

| Layer | May import | May not import |
|-------|------------|----------------|
| `core` | stdlib, pydantic | anything else |
| `db` | `core`, sqlalchemy | `plugins`, `services`, `api`, `cli`, `host` |
| other domain packages (`sources`, `scoring`, `resume`, `llm`, `browser`, `agents`, `email`, `tasks`, `memory`) | `core`, `db` | `applications`, `plugins`, `services`, `api`, `cli`, `host` |
| `applications` | other domain packages, `core`, `db` | `plugins`, `services`, `api`, `cli`, `host` |
| `plugins` | contracts, `core` | `services`, `api`, `cli`, `host` |
| `services` | everything below it | nothing above it |
| `api`, `cli`, `host` | `services` | `plugins` directly |

## Core domain (`applyuminati.core`)

Pure Pydantic v2 models and value objects. No database, no HTTP, no vendor SDKs.

Key types:
- **`AssertionLevel`** — seven epistemic levels from `MODEL_SUGGESTION` to `VERIFIED`. Promotion up the ladder is a deliberate, recorded act.
- **`Claim`** — a statement with an assertion level and provenance. The unit that resume tailoring and questionnaire answering traffic in.
- **`Provenance`** — where a claim came from (resume import, job source, LLM, user input) and when.
- **`CareerProfile`** — the canonical record, wrapping JSON Resume with claims, metrics, STAR stories, wording preferences, eligibility, targets, and strategy.
- **`Job`** — one real-world opening, however many sources reported it. Deduplicated by identity key, not URL.
- **`FitScore`** — structured, inspectable verdict with 11 dimensions, evidence, missing requirements, and uncertainties.
- **`Application`** — the user's pursuit of one job, with an append-only event log.

## Plugin contracts

Each extension point defines a Protocol and a Registry:

| Extension point | Protocol | Registry | Entry-point group |
|-----------------|-----------|-----------|-------------------|
| Job sources | `JobSource` | `SOURCE_REGISTRY` | `applyuminati.sources` |
| Application drivers | `ApplicationDriver` | `APPLICATION_DRIVER_REGISTRY` | `applyuminati.application_drivers` |
| LLM providers | `LLMProvider` | `LLM_REGISTRY` | `applyuminati.llm` |
| Browser backends | `BrowserBackend` | `BROWSER_REGISTRY` | `applyuminati.browsers` |
| Agent runtimes | `AgentBackend` | `AGENT_REGISTRY` | `applyuminati.agents` |
| Email providers | `EmailProvider` | `EMAIL_REGISTRY` | `applyuminati.email` |

Adding a plugin means implementing the protocol and registering a `PluginDescriptor`. No workflow code changes.

## Scoring

The deterministic engine (`scoring/engine.py`) always runs, needs no LLM, and produces a fully inspectable `FitScore`:

1. Each of 11 dimensions is scored 0–1 by a pure function.
2. Dimensions are weighted (weights normalise at aggregation time).
3. A `BlockerSeverity.HARD` missing requirement caps overall at 0.25 and forces `SKIP`.
4. Recommendation: `SKIP` below `strategy.skip_below_score`, `APPLY` above `strategy.minimum_fit_score` with sufficient confidence, else `INVESTIGATE`.

The optional LLM pass (`scoring/llm_pass.py`) may only adjust dimensions by ±0.2, add uncertainties, and add missing requirements. It may NOT set the overall score, invent dimensions, remove hard blockers, or change the recommendation. These limits are enforced in Python, not by prompt.

## Resume generation and the fabrication guard

Tailoring (`resume/tailor.py`) has a deterministic path (reorder, select, never change facts) and an optional LLM path (rewrite existing claims more clearly). Every output passes through `FabricationGuard` (`resume/guard.py`), a pure-Python check that detects:

- Employers, titles, institutions, certifications not in the profile → HARD
- Date ranges that differ from canonical → HARD
- Numeric metrics not in the profile's metric ledger → HARD (the highest-value check)
- Technologies not in the profile's skills → SOFT
- Banned phrases → SOFT

A HARD violation makes `ok=False`; the caller discards the LLM output and falls back to the deterministic result.

## Application lifecycle

20 states with a transition table enforced by `ApplicationMachine`. The event log is the record; the `state` column is a cache. `replay()` rebuilds state from the event log, proving the cache is derivable.

Idempotency: `submission_fingerprint` is over `(profile, company, title, location)`, NOT the URL — so the same role reached through two sources fingerprints identically.

## Memory

Nine categories, never raw prompt transcripts. The store enforces a hard rule: a machine-driven write at `VERIFIED` or `USER_APPROVED` is refused without `by_user=True`. Learning signals from user edits become writing-memory at `PREFERENCE` level, never canonical fact overwrites.

## Self-healing

Failures are classified into `FailureCategory` values that drive recovery policy:
- `TRANSIENT_NETWORK` → retry with backoff
- `RATE_LIMITED` → retry after `Retry-After`
- `EXTRACTION_DRIFT` → try the next registered strategy
- `AUTH_REQUIRED`, `HUMAN_CHALLENGE` → block, never retry
- `INVALID_MODEL_OUTPUT` → retry up to 2, then fail
- `RESOURCE_GONE`, `DUPLICATE_ACTION` → terminal immediately

A hard loop guard prevents repeating a strategy already in `attempted_strategies`.

## Browser automation

Ego Lite is the preferred backend (macOS, subprocess-driven, inherits the user's real logins). Playwright is the portable fallback. Both implement `BrowserBackend` / `BrowserSession` with semantic actions (navigate, observe, fill, upload, click, tabs, download, checkpoint, handoff).

A backend owns the browser process; a session owns one isolated context and the tabs inside it. Closing a session closes its context and leaves every other session running. Backends advertise only the operations they actually implement — Ego Lite declines tabs and downloads — and a Browser Host advertises the narrower intersection of its backend's capabilities with what its dispatcher will carry out. Files a site sends are written under `<data_dir>/downloads` with a name derived from the site's suggestion rather than taken from it.

**Never:** CAPTCHA solving, fingerprint spoofing, stealth techniques, or access-control evasion. When automation is blocked, the condition is detected and reported; the user is asked to take over.

## Persistence

SQLite via `aiosqlite` today, PostgreSQL via `asyncpg` later. JSON columns become `JSONB` on Postgres automatically. No dialect-specific DDL. Migrations through Alembic with `render_as_batch` for SQLite ALTER TABLE support.

## Patterns adopted from reference projects

- Evidence layer with epistemic levels (Remotivated/job-hunt-skills)
- Adapters that never raise, failures as data (abdulrbasit/job-hunter)
- Per-ATS workflow memory (ApplyPilot — ideas only, AGPL-3.0)
- Deterministic prefilter + LLM judge (mirror, job-hunt-agent)
- Versioned prompts stamped on output (mirror)
- Semantic snapshot + stable-locator addressing (ego-lite)
- Human handoff protocol (ego-lite, jobclaw-skills)
- Canonical enums with a transition function (job-hunter-team, jobclaw-skills)
- Two-tier memory: per-entity content + abstract style rules (mirror)
- Drafter/reviewer separation with grounding (open-career-skills)

## Intentional deviations

- **Single Python distribution** instead of a monorepo with separate packages — boundaries enforced by import-linter, not by packaging.
- **Bootstrap in plugin packages**, not contract packages — the contract must not import its own adapters.
- **No agent framework for deterministic fan-out** — `asyncio.gather` is enough; LLM is reserved for judgment.
- **Programmatic fabrication guard**, not a prompt instruction — anything load-bearing needs a code check.
- **No vector database until retrieval quality demands it** — keyword + recency retrieval is the documented seam.
- **No Redis, Celery, or Kafka** — a SQLite-backed task queue is sufficient for local-first.

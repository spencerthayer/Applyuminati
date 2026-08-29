# Contributing to Applyuminati

## Development setup

```bash
git clone https://github.com/spencerthayer/Applyuminati.git
cd Applyuminati
uv sync --all-extras --dev
cd apps/web && npm install && cd ..
```

## Running checks

```bash
make test          # pytest (offline only)
make lint          # ruff
make format        # ruff format
make typecheck      # pyright
make imports       # import-linter
```

Frontend:

```bash
cd apps/web
npm run lint
npm run typecheck
npm run test
npm run build
```

## Architecture rules

1. **Core domain logic is vendor-neutral.** `applyuminati.core` must not import httpx, sqlalchemy, fastapi, playwright, or any plugin.
2. **Plugins depend on contracts, not the reverse.** `applyuminati.sources` must not import `applyuminati.plugins`.
3. **UI and CLI use the same services.** No business logic in `api/` or `cli/`.
4. **LLM output is untrusted input.** Validate against a Pydantic schema; never return a half-parsed dict.
5. **Models do not own canonical truth.** A memory record is not a fact; an inference is not a verified claim.
6. **Every factual career claim has provenance.** The fabrication guard enforces this.
7. **A failure must remain inspectable.** Capture structured failure information, don't swallow it.
8. **Workflows are resumable.** Tasks carry `resume_state`.
9. **Application actions are idempotent.** The fingerprint guard prevents duplicate submissions.
10. **External integrations degrade gracefully.** A missing browser, LLM, or email provider never crashes the run.

These rules are enforced by import-linter contracts in `pyproject.toml` and checked in CI.

## Adding a job source

1. Create `src/applyuminati/plugins/sources/your_source.py`.
2. Implement the `JobSource` protocol (metadata, health, discover, verify).
3. Call `build_job()` from `applyuminati.sources.normalize` — never construct a `Job` by hand.
4. Expose `PLUGIN = source_plugin(...)`.
5. Register it in `plugins/sources/__init__.py:register_sources()`.
6. Add an entry point in `pyproject.toml` under `[project.entry-points."applyuminati.sources"]`.
7. Write tests using `respx` to mock HTTP responses.

## Adding an LLM provider

1. Create `src/applyuminati/plugins/llm/your_provider.py`.
2. Implement the `LLMProvider` protocol (metadata, health, complete, complete_structured, stream, aclose).
3. Use `httpx.AsyncClient`, no vendor SDK.
4. Expose `PLUGIN = llm_plugin(...)`.
5. Register it in `plugins/llm/__init__.py:register_llm_providers()`.

## Adding a browser backend

1. Create `src/applyuminati/plugins/browsers/your_backend.py`.
2. Implement `BrowserBackend` and `BrowserSession`.
3. Use the shared helpers in `plugins/browsers/shared.py` for condition detection and control parsing.
4. Expose `PLUGIN = browser_plugin(...)`.
5. Register it in `plugins/browsers/__init__.py:register_browsers()`.

## Writing prompts

Prompts live in `src/applyuminati/llm/prompts/`. Each prompt:
- Has an `id`, `version`, and `output_schema`.
- Uses `string.Template.substitute` (raises on missing variables).
- System message states that fabrication is a failure and "unknown" is acceptable.
- Is registered at import time via `register()`.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(scope): description
fix(scope): description
docs(scope): description
```

## License

By contributing, you agree that your contributions are licensed under Apache-2.0.

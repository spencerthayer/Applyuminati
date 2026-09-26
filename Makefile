.PHONY: help install dev api web test lint format typecheck imports migrate revision dist parity docker-build docker-up docker-down clean
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install Python dependencies
	uv sync --all-extras --dev

dev: ## Start API and web dev servers
	@echo "Run 'make api' and 'make web' in separate terminals"

api: ## Start the FastAPI dev server
	uv run uvicorn applyuminati.api.app:app --reload --port 8000

web: ## Start the Vite dev server
	cd apps/web && npm run dev

test: ## Run Python tests (offline)
	uv run pytest -m "not network and not browser" --cov=src/applyuminati

lint: ## Run ruff lint
	uv run ruff check .

format: ## Run ruff format
	uv run ruff format .

typecheck: ## Run pyright
	uv run pyright

imports: ## Check import-linter contracts
	uv run lint-imports

migrate: ## Run database migrations
	uv run alembic upgrade head

revision: ## Create a new migration
	@read -p "Migration message: " msg; uv run alembic revision --autogenerate -m "$$msg"

docker-build: ## Build the Docker image
	docker build -t applyuminati:dev .


docker-up: ## Start the Docker Compose stack (dev)
	docker compose -f docker-compose.dev.yml up --build

docker-down: ## Stop the Docker Compose stack
	docker compose -f docker-compose.dev.yml down


# `uv build` alone is not enough: it happily produces a wheel that registers
# no console script, declares no dependencies, or ships a pyproject with no
# Homepage, and every one of those fails on the user's machine rather than
# here. This check is what makes the artifact testable before it is uploaded.
dist: ## Build the wheel and sdist, then check their metadata
	uv build
	@uv run --no-project python -c "import pathlib, tomllib, zipfile; \
project = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']; \
missing = [k for k in ('Homepage', 'Source', 'Issues') if k not in project.get('urls', {})]; \
assert not missing, f'pyproject is missing [project.urls] entries: {missing}'; \
wheels = sorted(pathlib.Path('dist').glob('*.whl')); \
sdists = sorted(pathlib.Path('dist').glob('*.tar.gz')); \
assert wheels, 'uv build produced no wheel'; \
assert sdists, 'uv build produced no sdist'; \
zf = zipfile.ZipFile(wheels[0]); \
entry = zf.read(next(n for n in zf.namelist() if n.endswith('.dist-info/entry_points.txt'))).decode(); \
assert 'applyuminati =' in entry, 'wheel registers no applyuminati console script'; \
metadata = zf.read(next(n for n in zf.namelist() if n.endswith('.dist-info/METADATA'))).decode(); \
assert 'Requires-Dist:' in metadata, 'wheel declares no dependencies; an undeclared one is an ImportError on first run'; \
assert 'Requires-Python: ' + project['requires-python'] in metadata, 'wheel does not carry requires-python'; \
print(f'ok: {wheels[0].name}, {sdists[0].name}, console script and dependencies present')"

clean: ## Remove build artifacts
	rm -rf .data .pytest_cache .ruff_cache src/applyuminati.egg-info

# The markdown view of the surface manifest is generated, not written by hand,
# so it cannot disagree with the machine-readable one. Imports the manifest
# only: the tui extra is not needed to describe the TUI.
parity: ## Regenerate docs/parity.md from the surface parity manifest
	@uv run python -c "from pathlib import Path; from applyuminati.surface_parity import render_markdown; Path('docs/parity.md').write_text(render_markdown(), encoding='utf-8')"
	@cat docs/parity.md

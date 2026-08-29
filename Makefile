.PHONY: help install dev api web test lint format typecheck imports migrate revision docker-build docker-up docker-down clean

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

clean: ## Remove build artifacts
	rm -rf .data .pytest_cache .ruff_cache src/applyuminati.egg-info

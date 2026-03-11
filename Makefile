.PHONY: help install run lint format format-check typecheck test test-integration test-web-sandbox build check clean coverage docker-build

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies
	pip install -e ".[dev]" -e "../cowork-platform[sdk]"

run: ## Run the service locally with auto-reload
	set -a && [ -f .env ] && . .env; set +a && uvicorn session_service.main:app --reload --port 8000

lint: ## Run linter
	ruff check src/ tests/

format: ## Auto-format code
	ruff format src/ tests/
	ruff check --fix src/ tests/

format-check: ## Check formatting without modifying
	ruff format --check src/ tests/

typecheck: ## Run type checker
	mypy src/

test: ## Run unit tests
	pytest -m "unit or not (service or integration)" -x -q

test-integration: ## Run service/integration tests (requires LocalStack)
	pytest -m "service or integration" -x -q

build: ## Build package
	python -m build

check: lint format-check typecheck test ## CI gate: lint + format-check + typecheck + test

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

coverage: ## Run tests with coverage
	coverage run -m pytest -m "unit or not service" -x -q
	coverage report
	coverage html

test-web-sandbox: ## Run web sandbox E2E integration tests (requires running services)
	python scripts/test-web-sandbox.py

docker-build: ## Build Docker image
	docker build -t cowork-session-service:latest .

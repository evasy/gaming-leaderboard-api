# Developer entrypoints. Everything CI runs, you can run locally with one word.
.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV        ?= .venv
PY          ?= $(VENV)/bin/python
PIP         ?= $(VENV)/bin/pip
PYTEST      ?= $(VENV)/bin/pytest
RUFF        ?= $(VENV)/bin/ruff
MYPY        ?= $(VENV)/bin/mypy
UVICORN     ?= $(VENV)/bin/uvicorn
PORT        ?= 8080
BASE_URL    ?= http://localhost:$(PORT)
TEST_REDIS_URL ?= redis://localhost:6379/15

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate: pyproject.toml
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@touch $(VENV)/bin/activate

.PHONY: install
install: $(VENV)/bin/activate ## Create the venv and install dependencies

.PHONY: run
run: install ## Run the API locally (in-memory backend)
	$(UVICORN) app.main:app --reload --port $(PORT)

.PHONY: run-redis
run-redis: install ## Run the API locally against Redis
	LEADERBOARD_STORE_BACKEND=redis $(UVICORN) app.main:app --reload --port $(PORT)

.PHONY: test
test: install ## Run the full test suite (both backends if Redis is up)
	TEST_REDIS_URL=$(TEST_REDIS_URL) $(PYTEST) -q

.PHONY: cov
cov: install ## Run tests with a coverage report
	TEST_REDIS_URL=$(TEST_REDIS_URL) $(PYTEST) -q --cov=app --cov-report=term-missing

.PHONY: lint
lint: install ## Lint and check formatting
	$(RUFF) check .
	$(RUFF) format --check .

.PHONY: fmt
fmt: install ## Auto-fix lint and formatting
	$(RUFF) check --fix .
	$(RUFF) format .

.PHONY: types
types: install ## Type-check
	$(MYPY)

.PHONY: check
check: lint types test ## Everything CI runs

.PHONY: smoke
smoke: ## Smoke test a running instance (BASE_URL=...)
	./scripts/smoke.sh $(BASE_URL)

.PHONY: bench
bench: install ## Quick latency/throughput probe against a running instance
	$(PY) scripts/bench.py --base-url $(BASE_URL)

.PHONY: up
up: ## Run API + Redis via docker compose
	docker compose up --build

.PHONY: down
down: ## Tear down the compose stack
	docker compose down -v

.PHONY: clean
clean: ## Remove caches and the venv
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

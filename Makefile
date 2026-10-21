# RCM platform task runner (CLAUDE.md section 10).
#
# `make verify` is the single gate. It is what CI runs and what the Definition of
# Done (CLAUDE.md section 9) means by "green locally". Nothing in it may be skipped or
# marked continue-on-error.

ifeq ($(OS),Windows_NT)
SHELL := C:/Program Files/Git/usr/bin/bash.exe
else
SHELL := /bin/bash
endif
.SHELLFLAGS := -eu -o pipefail -c

VENV    := .venv
ifeq ($(OS),Windows_NT)
BIN     := $(VENV)/Scripts
PY      := py -3.12
else
BIN     := $(VENV)/bin
PY      := python3.12
endif
PYTHON  := $(BIN)/python
PIP     := $(BIN)/pip
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help venv install up down migrate seed openapi \
        lint typecheck test test-unit test-property test-integration \
        test-security test-contract verify clean frontend-install frontend-lint

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# --- environment ------------------------------------------------------------

venv: ## Create the 3.12 virtualenv
	$(PY) -m venv $(VENV)

install: venv ## Install the project and dev tooling
	# `pip install --upgrade pip` via the pip shim fails on Windows: pip cannot
	# replace its own running executable. `python -m pip` can.
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"
	$(BIN)/pre-commit install

frontend-install: ## Install frontend dependencies
	cd frontend && npm ci

# --- local stack ------------------------------------------------------------

up: ## Bring up Postgres, Redis, API and workers
	$(COMPOSE) up -d --wait
	@echo "stack up: api http://localhost:8000/health"

down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

migrate: ## Run Alembic migrations to head
	$(BIN)/alembic upgrade head

seed: ## Generate and load the synthetic corpus
	$(PYTHON) -m tools.synthetic --config tools/synthetic/config/config.yaml
	$(PYTHON) -m tools.synthetic.validate

seed-smoke: ## Generate the small corpus the tests use
	$(PYTHON) -m tools.synthetic --config tools/synthetic/config/smoke.yaml
	$(PYTHON) -m tools.synthetic.validate

openapi: ## Regenerate the OpenAPI schema and the frontend client
	$(PYTHON) -m rcm.api.export_openapi > frontend/src/api/openapi.json
	cd frontend && npm run generate:api

# --- quality gates ----------------------------------------------------------

lint: ## Ruff, import boundaries, frontend lint
	$(BIN)/ruff check src tests tools
	$(BIN)/ruff format --check src tests tools
	$(BIN)/lint-imports --config .importlinter

typecheck: ## mypy strict
	$(BIN)/mypy

test-unit: ## Unit tests
	$(BIN)/pytest tests/unit --cov --cov-report=term-missing

test-property: ## Hypothesis property tests
	$(BIN)/pytest tests/property

test-integration: ## Integration tests (needs `make up`)
	$(BIN)/pytest tests/integration -m integration

test-synthetic: ## Generator contract: the corpus contains every edge case M1 needs
	$(PYTHON) -m pytest tests/unit/test_synthetic_generator.py -q

test-security: ## Security control tests (CLAUDE.md section 6.3)
	$(BIN)/pytest tests/security -m security

test: test-unit test-property test-integration test-security ## All Python suites

# The order mirrors CI (CLAUDE.md section 7.4). Fail fast: .SHELLFLAGS sets -e, and each
# recipe line is a separate shell, so the first failure stops the target.
verify: lint typecheck test ## THE GATE: lint + types + every suite
	@echo ""
	@echo "verify: PASS"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

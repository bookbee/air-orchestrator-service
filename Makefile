VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
UVICORN := $(VENV)/bin/uvicorn
COMPOSE ?= docker compose

HOST    ?= 0.0.0.0
PORT    ?= 8081

SOURCES := src tests

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@echo "air-platform — make <target>"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Overridable: HOST=$(HOST) PORT=$(PORT)"
	@echo "Needs air-infra on :8080 — see ../air-infra (make up)."

# ---- dev inner loop --------------------------------------------------------
$(PY):
	python3.12 -m venv $(VENV)

.PHONY: install
install: $(PY) ## Create .venv and install the package
	$(PIP) install --upgrade pip
	$(PIP) install -e "."

.PHONY: dev
dev: $(PY) ## Install with every extra plus the development toolchain
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[all,dev]"

.PHONY: env
env: ## Create .env from .env.example, if absent
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it alone."; \
	else \
		cp .env.example .env; \
		echo "Wrote .env. It ships a development API key per channel; see the file."; \
	fi

.PHONY: run
run: ## Run the service with reload (needs air-infra on :8080)
	$(UVICORN) air_platform.main:app --reload --host $(HOST) --port $(PORT)

.PHONY: test
test: ## Run tests
	$(PYTEST)

.PHONY: cov
cov: ## Run tests with a coverage report
	$(PYTEST) --cov=air_platform --cov-report=term-missing

.PHONY: lint
lint: ## Ruff lint
	$(PY) -m ruff check $(SOURCES)

.PHONY: fmt
fmt: ## Ruff format and autofix
	$(PY) -m ruff format $(SOURCES)
	$(PY) -m ruff check --fix $(SOURCES)

.PHONY: typecheck
typecheck: ## Mypy (strict)
	$(PY) -m mypy src

.PHONY: check
check: lint typecheck test ## Lint + typecheck + test

.PHONY: openapi
openapi: ## Write the OpenAPI document to docs/openapi.json
	$(PY) -c "import json, pathlib; from air_platform.main import create_app; \
		pathlib.Path('docs/openapi.json').write_text(json.dumps(create_app().openapi(), indent=2) + '\n')"
	@echo "Wrote docs/openapi.json"

# ---- container -------------------------------------------------------------
.PHONY: up
up: ## Start this service in docker (expects air-infra's stack already up)
	$(COMPOSE) up -d --build
	@echo "platform: http://localhost:$(PORT)/v1/health"

.PHONY: down
down: ## Stop the container
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail container logs
	$(COMPOSE) logs -f platform

.PHONY: clean
clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

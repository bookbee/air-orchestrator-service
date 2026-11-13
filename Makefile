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
	@echo "air-orchestrator-service — make <target>"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Overridable: HOST=$(HOST) PORT=$(PORT)"
	@echo "A real answer needs air-model-gateway on :8083 — see ../air-model-gateway (make up)."
	@echo "make up additionally needs air-infra's air-net — see ../air-infra."

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
run: ## Run the service with reload (a real answer needs air-model-gateway on :8083)
	$(UVICORN) air_orchestrator_service.main:app --reload --host $(HOST) --port $(PORT)

.PHONY: test
test: ## Run tests
	$(PYTEST)

.PHONY: cov
cov: ## Run tests with a coverage report
	$(PYTEST) --cov=air_orchestrator_service --cov-report=term-missing

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

.PHONY: trace
trace: ## Check every spec requirement names a test that exists
	$(PY) tools/trace.py

.PHONY: prompt-review
prompt-review: ## Run the customer-care review set against a live service (paid model calls)
	$(PY) tools/prompt_review.py --base-url $(or $(BASE_URL),http://127.0.0.1:8081) \
		--key $(or $(KEY),airo_local_customer_key) $(if $(ONLY),--only $(ONLY))

.PHONY: spec
spec: ## Scaffold a new spec: make spec NAME=my-feature
	@test -n "$(NAME)" || { echo "usage: make spec NAME=my-feature"; exit 1; }
	@n=$$(printf '%04d' $$(( $$(ls -d docs/specs/[0-9]* 2>/dev/null | wc -l) + 1 ))); \
	dir="docs/specs/$$n-$(NAME)"; \
	test ! -d "$$dir" || { echo "$$dir already exists"; exit 1; }; \
	mkdir -p "$$dir"; \
	cp docs/specs/templates/spec.md "$$dir/spec.md"; \
	echo "Wrote $$dir/spec.md — fill it in, then 'make trace'."; \
	echo "Add plan.md and tasks.md from docs/specs/templates/ before implementing."

.PHONY: check
check: lint typecheck test trace ## Lint + typecheck + test + spec traceability

.PHONY: openapi
openapi: ## Write the OpenAPI document to docs/openapi.json
	$(PY) -c "import json, pathlib; from air_orchestrator_service.main import create_app; \
		pathlib.Path('docs/openapi.json').write_text(json.dumps(create_app().openapi(), indent=2) + '\n')"
	@echo "Wrote docs/openapi.json"

# ---- container -------------------------------------------------------------
# `air-net` is air-infra's shared network and belongs to air-infra, not here.
# Creating it ourselves would work once and then hand a service-name collision to
# whoever started the real stack afterwards, so this only ever checks.
.PHONY: require-air-net
require-air-net:
	@docker network inspect air-net >/dev/null 2>&1 || { \
		echo "air-net is not up — air-infra owns it, and this container joins it to reach air-infra."; \
		echo; \
		echo "  cd ../air-infra && make up"; \
		echo; \
		echo "Or run this service on the host instead, where it reaches both on localhost:"; \
		echo; \
		echo "  make run"; \
		echo; \
		exit 1; \
	}

.PHONY: up
up: require-air-net ## Start this service in docker (needs air-infra's stack up)
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

# The network existing does not mean the gateway behind it is up — `air-net`
# outlives `docker compose down`. So rather than assert anything, report what the
# service itself says: /v1/ready is the honest answer, and printing it here stops a
# green "Started" from being mistaken for a working stack.
.PHONY: status
status: ## Report this service's own readiness
	@sleep 2
	@printf 'health:   http://localhost:$(PORT)/v1/health\n'
	@code=$$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$(PORT)/v1/ready 2>/dev/null); \
	if [ "$$code" = "200" ]; then \
		printf 'ready:    yes — air-model-gateway serves this service\n'; \
	elif [ "$$code" = "503" ]; then \
		printf 'ready:    NO (503) — air-model-gateway cannot serve this service (down,\n'; \
		printf '          token rejected, or model class unavailable — see /v1/ready).\n'; \
		printf '          The service is up and will serve as soon as that is fixed;\n'; \
		printf '          no restart needed.\n'; \
		printf '          Start it with: cd ../air-model-gateway && make up\n'; \
	else \
		printf 'ready:    could not probe (curl said "%s") — try: make logs\n' "$$code"; \
	fi

.PHONY: down
down: ## Stop the container
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail container logs
	$(COMPOSE) logs -f air-orchestrator-service

.PHONY: clean
clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

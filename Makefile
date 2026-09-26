# Jarvis: everyday commands. Run them in Ubuntu (WSL) from the repo folder.
# `make` on its own lists them.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

# 1 when Docker can hand your NVIDIA GPU to containers. Override with GPU=0 or GPU=1.
GPU ?= $(shell scripts/wsl/detect-gpu.sh 2>/dev/null || echo 0)
COMPOSE := docker compose -f docker-compose.yml$(if $(filter 1,$(GPU)), -f compose.gpu.yml)
IMAGE := jarvis-core:local
SERVICE ?= core
RUNS ?= 5
BACKUP_DIR := data/backups

require_env = @test -f .env || { echo "No .env yet. Run: make secrets"; exit 1; }
require_running = @$(COMPOSE) ps --status running --services 2>/dev/null | grep -qx $(1) || { echo "The $(1) service isn't running. Start Jarvis with: make up"; exit 1; }

.PHONY: help
help: ## List the commands
	@echo "Jarvis. Usage: make <command>"
	@awk 'BEGIN { FS = ":.*## " } /^## / { printf "\n%s\n", substr($$0, 4) } \
		/^[a-z][a-z0-9-]*:.*## / { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

## Running Jarvis

.PHONY: docker-ready
docker-ready: # Waits up to a minute for Docker (it starts a few seconds after WSL does)
	@for _ in $$(seq 1 60); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; \
		echo "Docker isn't running. Try: sudo systemctl start docker (and if you were just added"; \
		echo "to the docker group, open a new terminal)."; exit 1

.PHONY: secrets
secrets: docker-ready ## Create .env (if missing) and generate its secrets; never overwrites yours
	docker build -t $(IMAGE) .
	docker run --rm --user "$$(id -u):$$(id -g)" -v "$(CURDIR):/work" -w /work $(IMAGE) \
		jarvis secrets --env-file /work/.env

.PHONY: up
up: docker-ready ## Start Jarvis (builds it the first time) and wait until it answers
	$(require_env)
	$(COMPOSE) up -d --remove-orphans --wait --wait-timeout 300
	@echo "Jarvis is up. First time? Get your setup code with: make setup-token"
	@echo "GPU for the local model: $(if $(filter 1,$(GPU)),yes,no (make doctor explains))"

.PHONY: boot-up
boot-up: # Used by the jarvis systemd service at boot
	@if [ ! -f .env ]; then echo "Jarvis isn't set up yet (no .env); not starting."; exit 0; fi
	$(COMPOSE) up -d --remove-orphans

.PHONY: down
down: ## Stop Jarvis and remove its containers (your data stays)
	$(COMPOSE) down

.PHONY: stop
stop: ## Stop Jarvis, keeping its containers
	$(COMPOSE) stop

.PHONY: restart
restart: ## Restart the Jarvis core (after editing .env or config/)
	$(require_env)
	$(COMPOSE) up -d --force-recreate --wait --wait-timeout 300 core

.PHONY: logs
logs: ## Follow the logs (SERVICE=core by default; e.g. make logs SERVICE=ollama)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

.PHONY: ps
ps: ## Show what's running
	$(COMPOSE) ps

.PHONY: setup-token
setup-token: ## Print a new first-run setup code (for registering your passkey)
	$(call require_running,core)
	$(COMPOSE) exec -T core jarvis setup-token

.PHONY: doctor
doctor: ## Check the whole setup and explain how to fix anything missing (ONLINE=1 also tests API keys)
	@scripts/wsl/host-doctor.sh || status=$$?; \
	echo "Jarvis"; \
	if $(COMPOSE) ps --status running --services 2>/dev/null | grep -qx core; then \
		$(COMPOSE) exec -T -e JARVIS_GPU_VRAM_GB="$$(scripts/wsl/detect-gpu.sh --vram)" core \
			jarvis doctor $(if $(ONLINE),--online) || status=$$?; \
	else \
		echo "✘ Jarvis isn't running"; echo "    fix: make up"; status=1; \
	fi; \
	exit $${status:-0}

.PHONY: pull-models
pull-models: ## Download the AI models: config/models.yaml (Ollama) and config/voice.yaml (speech)
	$(call require_running,ollama)
	$(call require_running,speech)
	@for model in $$($(COMPOSE) exec -T core jarvis local-models); do \
		echo "Pulling $$model"; $(COMPOSE) exec -T ollama ollama pull "$$model"; \
	done
	$(COMPOSE) exec -T core jarvis pull-speech-models

.PHONY: bench-voice
bench-voice: ## Time how fast Jarvis answers out loud (target: under 1.5 s; RUNS=5)
	$(call require_running,core)
	$(COMPOSE) exec -T core jarvis bench-voice --runs $(RUNS)

.PHONY: eval
eval: ## Score how well Jarvis sorts your email, against the ones you checked (target: 90%)
	$(call require_running,core)
	$(COMPOSE) exec -T core jarvis eval triage

.PHONY: update
update: docker-ready ## Get the latest Jarvis and restart it
	git pull --ff-only
	$(COMPOSE) build --pull core
	$(COMPOSE) up -d --remove-orphans --wait --wait-timeout 300

.PHONY: research-up
research-up: ## Also start SearXNG, the private web search (Phase 4)
	$(COMPOSE) --profile research up -d searxng

.PHONY: backup
backup: ## Save a database backup to data/backups (contains personal data: keep it safe)
	$(call require_running,postgres)
	@mkdir -p $(BACKUP_DIR) && chmod 700 $(BACKUP_DIR)
	@file="$(BACKUP_DIR)/jarvis-$$(date +%Y%m%d-%H%M%S).dump"; \
	(umask 077 && $(COMPOSE) exec -T postgres pg_dump -U jarvis -d jarvis --format=custom > "$$file"); \
	echo "Saved $$file ($$(du -h "$$file" | cut -f1))."; \
	echo "Restoring it also needs JARVIS_SECRET_KEY from .env: keep a copy of that key safe too."

.PHONY: restore
restore: ## Restore a backup: make restore FILE=data/backups/jarvis-....dump (replaces current data)
	@test -n "$(FILE)" && test -f "$(FILE)" || { echo "Usage: make restore FILE=data/backups/<file>.dump"; exit 1; }
	$(call require_running,postgres)
	@read -r -p "This replaces everything in Jarvis's database with $(FILE). Type 'restore' to go on: " answer; \
		[ "$$answer" = restore ] || { echo "Cancelled."; exit 1; }
	$(COMPOSE) stop core
	$(COMPOSE) exec -T postgres pg_restore -U jarvis -d jarvis --clean --if-exists --no-owner < "$(FILE)"
	$(COMPOSE) up -d core
	@echo "Restored. If this backup came from another install, copy its JARVIS_SECRET_KEY into .env too."

## Development (needs uv, pnpm and Docker)

.PHONY: install
install: ## Install the Python and web dependencies (and the voice pipeline's sentence data)
	cd core && uv sync --all-extras
	cd core && uv run jarvis fetch-text-data --dest ../data/nltk_data
	cd satellite && uv sync
	cd web && pnpm install --frozen-lockfile

.PHONY: dev-db
dev-db: ## Start a throwaway Postgres for tests on 127.0.0.1:5432
	docker compose -f compose.dev.yml up -d --wait

.PHONY: dev-db-down
dev-db-down: ## Remove the test Postgres
	docker compose -f compose.dev.yml down

.PHONY: dev-core
dev-core: ## Run the core for development (needs make dev-db; see scripts/dev/run-core.sh)
	scripts/dev/run-core.sh

.PHONY: dev-web
dev-web: ## Run the web app with hot reload on http://localhost:5173 (proxies /api to the core)
	cd web && pnpm dev

.PHONY: fmt
fmt: ## Format and auto-fix the code
	cd core && uv run ruff check --fix . && uv run ruff format .
	cd satellite && uv run ruff check --fix . && uv run ruff format .
	cd web && pnpm exec prettier --write .

.PHONY: lint
lint: ## Lint everything
	cd core && uv run ruff check . && uv run ruff format --check .
	cd satellite && uv run ruff check . && uv run ruff format --check .
	cd web && pnpm lint && pnpm format:check

.PHONY: typecheck
typecheck: ## Type-check everything
	cd core && uv run pyright
	cd satellite && uv run pyright
	cd web && pnpm typecheck

.PHONY: test
test: ## Run the unit and integration tests (needs make dev-db)
	cd core && uv run pytest
	cd satellite && uv run pytest
	cd web && pnpm test

.PHONY: injection
injection: ## Run the email prompt-injection suite alone (needs make dev-db)
	cd core && uv run pytest tests/integration/test_email_injection.py -q

.PHONY: e2e
e2e: ## Run the browser end-to-end tests (needs make dev-db)
	cd web && pnpm exec vite build && pnpm e2e

.PHONY: check
check: lint typecheck test ## Everything CI runs except the browser tests

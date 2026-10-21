# Company Brain — entry point for all operations. `make up` is `docker compose up -d` with your
# user id, so the files the brain writes in ./data stay yours; then open the setup wizard.

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

WIKI_MCP_DIR := wiki-mcp
PORTAL_DIR   := portal
TEST_IMAGE   := company-brain/brain:test
PORTAL_TEST_IMAGE := company-brain/portal:test
UV_IMAGE     := ghcr.io/astral-sh/uv:0.12.19-python3.13-trixie-slim
COMPOSE      := HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose
AS_ME        := -u "$$(id -u):$$(id -g)"

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stack

.PHONY: up down restart logs ps
up: ## Build and start the stack; then open the setup wizard (URL in `make logs s=brain`)
	$(COMPOSE) up -d --build
down: ## Stop the stack (the data folder is kept)
	$(COMPOSE) down
restart: ## Restart all services
	$(COMPOSE) restart
logs: ## Follow logs (make logs s=brain for one service)
	$(COMPOSE) logs -f $(s)
ps: ## Show service status
	$(COMPOSE) ps

# ---------------------------------------------------------------- operations

.PHONY: clients lint-wiki
clients: ## List OAuth clients (ARGS="revoke <client_id>" or "private <label> on|off|default")
	$(COMPOSE) exec -T $(AS_ME) brain wiki-mcp clients $(if $(ARGS),$(ARGS),list)
lint-wiki: ## Run the wiki linter now and print the report
	$(COMPOSE) exec -T $(AS_ME) brain wiki-mcp lint

# ---------------------------------------------------------------- development

.PHONY: test lock fmt
test: ## Lint, type-check and run the brain and portal test suites (in Docker)
	docker build -f $(WIKI_MCP_DIR)/Dockerfile --target test -t $(TEST_IMAGE) .
	docker run --rm $(TEST_IMAGE)
	docker build -f $(PORTAL_DIR)/Dockerfile --target test -t $(PORTAL_TEST_IMAGE) .
	docker run --rm $(PORTAL_TEST_IMAGE)
lock: ## Re-resolve wiki-mcp and portal dependencies (uv.lock)
	docker run --rm -u "$$(id -u):$$(id -g)" -e UV_CACHE_DIR=/tmp/uvcache \
		-v "$(CURDIR)/$(WIKI_MCP_DIR)":/w -w /w $(UV_IMAGE) uv lock
	docker run --rm -u "$$(id -u):$$(id -g)" -e UV_CACHE_DIR=/tmp/uvcache \
		-v "$(CURDIR)":/w -w /w/$(PORTAL_DIR) $(UV_IMAGE) uv lock
fmt: ## Auto-format wiki-mcp and portal sources with ruff
	for d in $(WIKI_MCP_DIR) $(PORTAL_DIR); do \
		docker run --rm -u "$$(id -u):$$(id -g)" -e UV_CACHE_DIR=/tmp/uvcache -e HOME=/tmp \
			-v "$(CURDIR)/$$d":/w -w /w $(UV_IMAGE) \
			sh -c 'uvx ruff@0.16.9 format . && uvx ruff@0.16.9 check --fix .' || exit 1; \
	done

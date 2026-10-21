# syntax=docker/dockerfile:1
# Multi-arch (amd64/arm64). Build context is the repository root:
#   docker build -f wiki-mcp/Dockerfile .
# Targets: `test` (lint + types + tests) and `runtime` (default).

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.13.13-slim-trixie AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# ---- test: dev deps, source and tests; `make test` runs this stage ----
FROM base AS test
COPY wiki-mcp/pyproject.toml wiki-mcp/uv.lock wiki-mcp/README.md ./
RUN uv sync --locked --no-install-project
COPY wiki-mcp/src ./src
COPY wiki-mcp/tests ./tests
COPY wiki-mcp/seed ./seed
COPY config/wiki-types.yaml ./config/wiki-types.yaml
RUN uv sync --locked
ENV UV_NO_SYNC=1
CMD ["sh", "-c", "uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest"]

# ---- build: production deps only ----
FROM base AS build
COPY wiki-mcp/pyproject.toml wiki-mcp/uv.lock wiki-mcp/README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY wiki-mcp/src ./src
RUN uv sync --locked --no-dev --no-editable

# ---- runtime ----
# Starts as root only to hand root-created bind folders to HOST_UID, then drops to that user
# (entrypoint.sh); compose grants just CHOWN, SETUID and SETGID for that.
FROM python:3.13.13-slim-trixie AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
COPY wiki-mcp/seed /app/seed
COPY config/wiki-types.yaml /app/config/wiki-types.yaml
COPY wiki-mcp/entrypoint.sh /app/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
EXPOSE 8080 8081 8090
ENTRYPOINT ["/app/entrypoint.sh"]

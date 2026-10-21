# syntax=docker/dockerfile:1
# Multi-arch (amd64/arm64). Build context is the repository root:
#   docker build -f portal/Dockerfile .
# Targets: `test` (lint + types + tests) and `runtime` (default).
# wiki-mcp is a path dependency (shared owner login, rate limits, audit, mode rules).

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.13.13-slim-trixie AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1
WORKDIR /src/portal

# ---- test: dev deps, source and tests; `make test` runs this stage ----
# Integration tests run a real wiki-mcp (needs git and the seed bundle) behind the portal.
FROM base AS test
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY wiki-mcp/seed /src/wiki-mcp/seed
COPY wiki-mcp/pyproject.toml wiki-mcp/README.md /src/wiki-mcp/
COPY wiki-mcp/src /src/wiki-mcp/src
COPY portal/pyproject.toml portal/uv.lock portal/README.md ./
RUN uv sync --locked --no-install-project
COPY portal/src ./src
COPY portal/tests ./tests
COPY docs /src/docs
COPY README.md /src/docs/README.md
COPY config /src/config
RUN uv sync --locked
ENV UV_NO_SYNC=1
CMD ["sh", "-c", "uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest"]

# ---- build: production deps only ----
FROM base AS build
COPY wiki-mcp/pyproject.toml wiki-mcp/README.md /src/wiki-mcp/
COPY wiki-mcp/src /src/wiki-mcp/src
COPY portal/pyproject.toml portal/uv.lock portal/README.md ./
RUN uv sync --locked --no-dev --no-install-project --no-editable
COPY portal/src ./src
RUN uv sync --locked --no-dev --no-editable

# ---- runtime ----
# Runs as the host user (compose sets `user:`), so the files it writes under
# ./secrets/providers and .generated/models stay owned by you. Nothing here needs root.
FROM python:3.13.13-slim-trixie AS runtime
COPY --from=build /src/portal/.venv /src/portal/.venv
COPY docs /app/docs
COPY README.md /app/docs/README.md
ENV PATH="/src/portal/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER 65534:65534
EXPOSE 3090
ENTRYPOINT ["portal"]

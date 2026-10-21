"""Fixtures for the integration tests: a real wiki-mcp on an ephemeral port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from wiki_mcp.config import Settings as WikiSettings
from wiki_mcp.internal import build_internal_app
from wiki_mcp.runtime import Runtime, create_runtime

from portal.wikiclient import WikiClient
from tests.conftest import _find, serve_app

PORTAL_TOKEN = "p" * 40


@pytest.fixture
async def wiki_runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    """wiki-mcp's runtime behind `wiki` (its assistant dream sessions, for one)."""
    settings = WikiSettings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "wiki-state",
        types_file=_find("config/wiki-types.yaml"),
        seed_dir=_find("wiki-mcp/seed"),
    )
    rt = await create_runtime(settings, run_background=False)
    yield rt
    await rt.close()


@pytest.fixture
async def wiki(wiki_runtime: Runtime) -> AsyncIterator[WikiClient]:
    rt = wiki_runtime
    app, _ = build_internal_app(
        rt.deps,
        token="l" * 40,
        client_name="librechat",
        scopes=["wiki:read"],
        allowed_hosts=["127.0.0.1:*"],
        portal_token=PORTAL_TOKEN,
    )
    async with serve_app(app) as url:
        client = WikiClient(url + "/api/portal/v1", PORTAL_TOKEN)
        yield client
        await client.close()

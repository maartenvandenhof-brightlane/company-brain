"""Google Drive by hand: the folder mode's export, downloaded through wiki-mcp's portal API."""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from wiki_mcp.folder import mirror
from wiki_mcp.internal import build_internal_app
from wiki_mcp.runtime import Runtime

from portal.app import build_app
from portal.config import Settings
from portal.wikiclient import WikiClient
from tests.conftest import login, serve_app
from tests.integration.conftest import PORTAL_TOKEN


@asynccontextmanager
async def portal(
    settings: Settings, rt: Runtime, drive: Path, *, signed_in: bool = True
) -> AsyncIterator[httpx2.AsyncClient]:
    app, _ = build_internal_app(
        rt.deps,
        token="l" * 40,
        client_name="librechat",
        scopes=["wiki:read"],
        allowed_hosts=["127.0.0.1:*"],
        portal_token=PORTAL_TOKEN,
        drive_dir=drive,
    )
    async with serve_app(app) as wiki_url:
        web = build_app(settings, wiki=WikiClient(wiki_url + "/api/portal/v1", PORTAL_TOKEN))
        async with serve_app(web) as url, httpx2.AsyncClient(base_url=url) as http:
            if signed_in:
                await login(http)
            yield http


def export(rt: Runtime, drive: Path) -> None:
    for rel, text in mirror(rt.store).items():
        (drive / "Brain" / rel).parent.mkdir(parents=True, exist_ok=True)
        (drive / "Brain" / rel).write_text(text, encoding="utf-8")


async def test_page_and_downloads(
    make_settings: Callable[..., Settings], wiki_runtime: Runtime, tmp_path: Path
) -> None:
    export(wiki_runtime, tmp_path / "drive")
    async with portal(make_settings(), wiki_runtime, tmp_path / "drive") as http:
        page = await http.get("/drive")
        assert page.status_code == 200
        assert "# Company Brain: start here" in page.text and "# Index" in page.text
        assert 'href="/drive/Brain.zip"' in page.text and 'class="button small copy"' in page.text
        assert 'href="/docs/CHAT_INSTRUCTIONS#mode-folder"' in page.text

        r = await http.get("/drive/Brain.zip")
        assert r.headers["content-type"] == "application/zip"
        assert r.headers["content-disposition"] == 'attachment; filename="Brain.zip"'
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert {"Brain/00 Start here.md", "Brain/00 Index.md"} <= set(names)

        r = await http.get("/drive/index.md")
        assert r.headers["content-disposition"] == 'attachment; filename="00 Index.md"'
        assert r.headers["content-type"].startswith("text/markdown")
        assert r.text.startswith("# Index")


async def test_without_an_export(
    make_settings: Callable[..., Settings], wiki_runtime: Runtime, tmp_path: Path
) -> None:
    async with portal(make_settings(), wiki_runtime, tmp_path / "drive") as http:
        page = await http.get("/drive")
        assert "Choose &#39;Google Drive by hand&#39; in the setup wizard first" in page.text
        r = await http.get("/drive/Brain.zip")
        assert r.status_code == 303 and r.headers["location"] == "/drive"


async def test_requires_sign_in(
    make_settings: Callable[..., Settings], wiki_runtime: Runtime, tmp_path: Path
) -> None:
    export(wiki_runtime, tmp_path / "drive")
    async with portal(make_settings(), wiki_runtime, tmp_path / "drive", signed_in=False) as http:
        for path in ("/drive", "/drive/Brain.zip", "/drive/start-here.md"):
            r = await http.get(path)
            assert r.status_code == 303 and r.headers["location"] == f"/login?next={path}"

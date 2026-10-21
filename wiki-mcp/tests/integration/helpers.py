from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp


@asynccontextmanager
async def serve_app(app: ASGIApp) -> AsyncIterator[str]:
    """Run an ASGI app on an ephemeral localhost port; yields its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", proxy_headers=False)
    server = uvicorn.Server(config)
    server.capture_signals = lambda: _null()  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
        if task.done():
            task.result()
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def _nullcm() -> AsyncIterator[None]:
    yield


class _Null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: object) -> None:
        return None


def _null() -> _Null:
    return _Null()


@asynccontextmanager
async def mcp_client(url: str, token: str) -> AsyncIterator[Client]:
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0)
        ) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        yield client


def structured(result: Any) -> dict[str, Any]:
    """Structured content of a tool result, checked against its JSON text twin."""
    assert not result.is_error, result.content
    data = result.structured_content
    text = json.loads(result.content[0].text)
    assert text == data
    return data  # type: ignore[no-any-return]

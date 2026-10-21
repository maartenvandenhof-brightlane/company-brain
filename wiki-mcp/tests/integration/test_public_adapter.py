"""The public MCP listener serves only while the saved mode includes MCP."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx2
import pytest

from tests.conftest import SEED_DIR, TYPES_FILE
from wiki_mcp.brain import TS_DNSNAME, BrainConfig, Mode
from wiki_mcp.config import Settings
from wiki_mcp.public import McpPublicAdapter, public_url
from wiki_mcp.runtime import create_runtime
from wiki_mcp.supervisor import Supervisor


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_mode_switches_the_public_listener(tmp_path: Path) -> None:
    port = _free_port()
    settings = Settings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "state",
        data_dir=tmp_path,
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
        public_host="127.0.0.1",
        public_port=port,
        PUBLIC_BASE_URL=f"http://127.0.0.1:{port}",
    )
    rt = await create_runtime(settings, run_background=False)
    sup = Supervisor([McpPublicAdapter(rt)])
    url = f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource"
    try:
        async with httpx2.AsyncClient() as http:
            await sup.apply(BrainConfig(Mode.MCP))
            assert (await http.get(url)).json()["resource"].endswith("/mcp")
            await sup.apply(BrainConfig(Mode.BOTH))  # still running, not restarted
            assert (await http.get(url)).status_code == 200
            await sup.apply(BrainConfig(Mode.GDRIVE))
            with pytest.raises(httpx2.ConnectError):
                await http.get(url)
            await sup.apply(BrainConfig(Mode.MCP))  # a fresh app starts again
            assert (await http.get(url)).status_code == 200
    finally:
        await sup.close()
        await rt.close()


async def test_no_public_url_means_no_listener(tmp_path: Path) -> None:
    settings = Settings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "state",
        data_dir=tmp_path,
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
    )
    rt = await create_runtime(settings, run_background=False)
    sup = Supervisor([McpPublicAdapter(rt)])
    await sup.apply(BrainConfig(Mode.MCP))  # a warning, not a crash; retried at the next save
    assert sup.status() == {"mcp": False}
    await sup.close()
    await rt.close()


async def test_the_sidecars_dns_name_starts_the_listener(tmp_path: Path) -> None:
    port = _free_port()
    settings = Settings(
        wiki_dir=tmp_path / "wiki",
        state_dir=tmp_path / "state",
        data_dir=tmp_path,
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
        public_host="127.0.0.1",
        public_port=port,
    )
    rt = await create_runtime(settings, run_background=False)
    mcp = McpPublicAdapter(rt)
    sup = Supervisor([mcp])
    follow = asyncio.create_task(mcp.follow(sup, every=0.05))
    dnsname = settings.tailscale_dir / TS_DNSNAME
    url = f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource"
    try:
        await sup.apply(BrainConfig(Mode.MCP))
        assert sup.status() == {"mcp": False} and "Tailscale" in sup.details()["mcp"]["problem"]
        dnsname.parent.mkdir()
        dnsname.write_text("evil.example.com\n")  # only a *.ts.net name becomes the issuer
        await asyncio.sleep(0.2)
        assert public_url(settings) is None and sup.status() == {"mcp": False}
        dnsname.write_text("company-brain.tail1.ts.net\n")
        async with httpx2.AsyncClient() as http:
            for _ in range(100):
                if sup.status()["mcp"]:
                    break
                await asyncio.sleep(0.05)
            meta = (await http.get(url)).json()
            assert meta["resource"] == "https://company-brain.tail1.ts.net/mcp"
            assert rt.store.public_base_url == "https://company-brain.tail1.ts.net"
            dnsname.write_text("renamed.tail1.ts.net\n")  # a new machine name: restarted
            for _ in range(100):
                if mcp.base == "https://renamed.tail1.ts.net" and sup.status()["mcp"]:
                    break
                await asyncio.sleep(0.05)
            meta = (await http.get(url)).json()
            assert meta["resource"] == "https://renamed.tail1.ts.net/mcp"
    finally:
        follow.cancel()
        await sup.close()
        await rt.close()

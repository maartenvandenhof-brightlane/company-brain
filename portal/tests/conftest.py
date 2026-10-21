from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from starlette.types import ASGIApp
from wiki_mcp.auth.passwords import hash_password

from portal.config import Settings
from portal.dreams.budget import Budget
from portal.usage import UsageLog

PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
ORIGIN = "http://portal.test"
PASSWORD_HASH = hash_password(PASSWORD)


def _find(rel: str) -> Path:
    here = Path(__file__).resolve().parents[1]
    for base in (here, here.parent):
        if (base / rel).exists():
            return base / rel
    raise FileNotFoundError(rel)


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "PORTAL_PUBLIC_URL": ORIGIN,
            "owner_password_hash": PASSWORD_HASH,
            "state_dir": tmp_path / "state",
            "wiki_api_token": "p" * 40,
            "docs_dir": _find("docs"),
            # nothing listens here: dashboard probes fail fast instead of resolving names
            "librechat_url": "http://127.0.0.1:9",
            "litellm_url": "http://127.0.0.1:9",
            "wiki_mcp_internal_url": "http://127.0.0.1:9",
            "wiki_mcp_public_url": "http://127.0.0.1:9",
            "keys_dir": tmp_path / "keys",
            "models_dir": tmp_path / "models",
            "usage_dir": tmp_path / "usage",
            "config_dir": _find("config"),
        }
        values.update(overrides)
        values["keys_dir"].mkdir(mode=0o700, exist_ok=True)
        values["models_dir"].mkdir(exist_ok=True)
        return Settings(**values)

    return make


@asynccontextmanager
async def serve_app(app: ASGIApp) -> AsyncIterator[str]:
    """Run an ASGI app on an ephemeral localhost port; yields its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", proxy_headers=False)
    server = uvicorn.Server(config)
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


_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_of(html: str) -> str:
    match = _CSRF.search(html)
    assert match, "no CSRF field on the page"
    return match.group(1)


async def login(http: httpx2.AsyncClient, *, totp: str = "") -> httpx2.Response:
    page = await http.get("/login")
    data = {"csrf": csrf_of(page.text), "username": "owner", "password": PASSWORD, "next": "/"}
    if totp:
        data["totp"] = totp
    return await http.post("/login", data=data, headers={"Origin": ORIGIN})


# USD per token, as in LiteLLM's public price list (model-prices.json).
PRICES = {
    "mistral/zai-glm-5-3": {"input_cost_per_token": 1.4e-06, "output_cost_per_token": 4.4e-06},
    "mistral/mistral-large-latest": {
        "input_cost_per_token": 5e-07,
        "output_cost_per_token": 1.5e-06,
    },
}


def priced_models(models_dir: Path, *, rate: float = 0.9) -> Path:
    """A rendered model directory as refresh_models.py leaves it: glm-5-3 and mistral-large
    served and priced, costs shown in EUR at `rate` per USD."""
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "litellm.yaml").write_text(
        "model_list:\n"
        "- model_name: glm-5-3\n  litellm_params: {model: mistral/zai-glm-5-3}\n"
        "- model_name: mistral-large\n  litellm_params: {model: mistral/mistral-large-latest}\n",
        encoding="utf-8",
    )
    (models_dir / "model-prices.json").write_text(json.dumps(PRICES), encoding="utf-8")
    (models_dir / "librechat.yaml").write_text(
        f"interface:\n  currency: {{code: EUR, rate: {rate}}}\n", encoding="utf-8"
    )
    return models_dir


def make_budget(state_dir: Path, models_dir: Path, **overrides: Any) -> Budget:
    values: dict[str, Any] = {
        "total_eur": 5.0,
        "automation_eur": 3.0,
        "cheap": "mistral-large",
        "strong": ["claude-opus-5-5", "gpt-sol-5.6", "glm-5-3"],
    }
    values.update(overrides)
    usage = UsageLog(state_dir.parent / "usage", models_dir)
    return Budget(state_dir, models_dir, usage, **values)

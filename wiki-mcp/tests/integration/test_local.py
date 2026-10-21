"""The local listener: setup state, the wizard and settings page, and the REST API."""

from __future__ import annotations

import json
import re
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from tests.conftest import SEED_DIR, TYPES_FILE, person
from tests.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from tests.integration.helpers import serve_app
from tests.unit.test_brain import FakeAdapter
from wiki_mcp.brain import (
    ADMIN_HASH,
    TS_AUTHKEY,
    TS_HOSTNAME,
    BrainConfig,
    Mode,
    load_config,
    read_secret,
)
from wiki_mcp.config import Settings, Trust
from wiki_mcp.google import GOOGLE_CLIENT, TASKS_SCOPE
from wiki_mcp.local import build_local_app
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.supervisor import Supervisor
from wiki_mcp.wiki.actor import Actor

TOKEN = "b" * 40
PASSWORD = "correct horse battery staple"
AUTHKEY = "tskey-auth-k1234CNTRL-abcdef"
OWNER = Actor("test", Trust.OWNER, frozenset({"wiki:read", "wiki:write", "wiki:private"}))


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = Settings(
        wiki_dir=tmp_path / "data" / "wiki",
        state_dir=tmp_path / "data" / "state",
        data_dir=tmp_path / "data",
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
        proposals_for="agent",  # the bearer token's agent writes become proposals
        internal_scopes=["wiki:read", "wiki:write"],
    )
    rt = await create_runtime(settings, run_background=False)
    yield rt
    await rt.close()


@pytest.fixture
def started() -> list[str]:
    return []


@pytest.fixture
async def http(runtime: Runtime, started: list[str]) -> AsyncIterator[httpx2.AsyncClient]:
    supervisor = Supervisor([FakeAdapter("mcp", {Mode.MCP, Mode.BOTH}, "mcp", started)])
    await supervisor.apply(load_config(runtime.settings.config_file))
    app = build_local_app(runtime, supervisor, api_token=TOKEN)
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as client:
        client.headers["Origin"] = url
        yield client


def csrf(html: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, html
    return found.group(1)


async def post(http: httpx2.AsyncClient, path: str, page: str, **fields: str) -> httpx2.Response:
    return await http.post(path, data={"csrf": csrf(page), **fields})


async def run_wizard(http: httpx2.AsyncClient) -> None:
    page = (await http.get("/setup")).text
    assert "Welcome" in page and "admin password" in page
    r = await post(http, "/setup/password", page, password=PASSWORD, confirm=PASSWORD)
    assert r.status_code == 303
    step1 = (await http.get("/setup")).text
    assert "Connectivity" in step1 and "Google Drive + inbox" in step1
    await post(http, "/setup/mode", step1, mode="both")
    step2 = (await http.get("/setup?step=settings")).text
    assert 'name="folder"' in step2 and 'name="authkey"' in step2 and "Connect Google" in step2
    assert "nodeAttrs" in step2 and 'value="company-brain"' in step2
    r = await post(
        http, "/setup/settings", step2, tunnel="tailscale", hostname="acme-brain", folder="Brain",
        inbox="tasks", prefix="[kb]", authkey=AUTHKEY,
    )  # fmt: skip
    assert r.headers["location"] == "/setup?step=check"
    step3 = (await http.get("/setup?step=check")).text
    assert "Wiki folder" in step3 and "Public MCP URL" in step3 and "Google account" in step3
    assert "waiting for the Tailscale sidecar" in step3  # no DNS name published yet
    r = await post(http, "/setup/start", step3)
    assert r.status_code == 303


async def test_setup_state_redirects_everything_to_the_wizard(
    http: httpx2.AsyncClient, runtime: Runtime
) -> None:
    health = (await http.get("/health")).json()
    assert health == {
        "status": "ok",
        "setup": True,
        "mode": None,
        "adapters": {"mcp": False},
        "details": {},
    }
    for method, path in (("GET", "/search?q=x"), ("GET", "/pages/x"), ("POST", "/ingest")):
        r = await http.request(method, path, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert (await http.get("/setup")).status_code == 200


async def test_wizard_saves_and_starts_without_restart(
    http: httpx2.AsyncClient, runtime: Runtime, started: list[str]
) -> None:
    await run_wizard(http)
    settings = runtime.settings
    assert load_config(settings.config_file) == BrainConfig.from_dict(
        {
            "mode": "both",
            "mcp": {"hostname": "acme-brain"},
            "gdrive": {"folder": "Brain", "inbox": "tasks", "prefix": "[kb]"},
        }
    )
    # The sidecar's login, in its own folder (never in config.yaml or secrets/).
    assert read_secret(settings.tailscale_dir, TS_AUTHKEY) == AUTHKEY
    assert read_secret(settings.tailscale_dir, TS_HOSTNAME) == "acme-brain"
    assert stat.S_IMODE((settings.tailscale_dir / TS_AUTHKEY).stat().st_mode) == 0o600
    assert AUTHKEY not in settings.config_file.read_text()
    assert read_secret(settings.secrets_dir, ADMIN_HASH).startswith("$argon2id$")
    assert PASSWORD not in settings.config_file.read_text()
    assert started == ["start mcp"]
    assert (await http.get("/health")).json()["mode"] == "both"
    page = (await http.get("/setup")).text  # now the settings page
    assert "Change settings" in page and "Brain" in page and "running" in page
    # Changing only the Drive settings leaves the MCP adapter alone.
    step2 = (await http.get("/setup?step=settings")).text
    await post(
        http, "/setup/settings", step2, tunnel="tailscale", hostname="acme-brain", folder="Other",
        inbox="keep", prefix="[kb]", authkey="",
    )  # fmt: skip
    assert read_secret(settings.tailscale_dir, TS_AUTHKEY) == AUTHKEY  # empty keeps the key
    await post(http, "/setup/start", (await http.get("/setup?step=check")).text)
    assert started == ["start mcp"] and load_config(settings.config_file).gdrive.folder == "Other"  # type: ignore[union-attr]
    await post(http, "/setup/mode", (await http.get("/setup?step=mode")).text, mode="gdrive")
    await post(http, "/setup/start", (await http.get("/setup?step=check")).text)
    assert started == ["start mcp", "stop mcp"]


async def test_wizard_guards(http: httpx2.AsyncClient, runtime: Runtime) -> None:
    page = (await http.get("/setup")).text
    short = await post(http, "/setup/password", page, password="short", confirm="short")
    assert "at least 12" in short.text and not read_secret(runtime.settings.secrets_dir, ADMIN_HASH)
    # No CSRF token, a foreign origin or a foreign Host: nothing happens.
    r = await http.post("/setup/password", data={"password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 303 and not read_secret(runtime.settings.secrets_dir, ADMIN_HASH)
    r = await http.post(
        "/setup/password",
        data={"csrf": csrf(page), "password": PASSWORD, "confirm": PASSWORD},
        headers={"Origin": "http://evil.example"},
    )
    assert not read_secret(runtime.settings.secrets_dir, ADMIN_HASH)
    assert (await http.get("/setup", headers={"Host": "evil.example"})).status_code == 400
    assert (await http.get("/setup", headers={"Host": "[::1]:8080"})).status_code == 200
    await run_wizard(http)
    # Signed out, the wizard asks for the password; the first-start form is gone for good.
    await post(http, "/setup/logout", (await http.get("/setup")).text)
    page = (await http.get("/setup")).text
    assert "Sign in" in page and "Welcome" not in page
    r = await post(http, "/setup/password", page, password="x" * 20, confirm="x" * 20)
    assert (await post(http, "/setup/login", page, password="x" * 20)).text.count("Wrong") == 1
    assert (await post(http, "/setup/login", page, password=PASSWORD)).status_code == 303
    assert "Change settings" in (await http.get("/setup")).text
    # A pasted key that is not one is rejected before it reaches the sidecar.
    step2 = (await http.get("/setup?step=settings")).text
    r = await post(http, "/setup/settings", step2, hostname="acme-brain", authkey="x\nrm -rf")
    assert "tskey-auth" in r.text
    assert read_secret(runtime.settings.tailscale_dir, TS_AUTHKEY) == AUTHKEY
    r = await post(http, "/setup/settings", step2, hostname="Not A Name", authkey="")
    assert "hostname must be" in r.text


async def test_wizard_folder_mode(
    http: httpx2.AsyncClient, runtime: Runtime, started: list[str]
) -> None:
    page = (await http.get("/setup")).text
    await post(http, "/setup/password", page, password=PASSWORD, confirm=PASSWORD)
    step1 = (await http.get("/setup")).text
    assert "Google Drive by hand" in step1
    await post(http, "/setup/mode", step1, mode="folder")
    step2 = (await http.get("/setup?step=settings")).text
    assert (
        "a download of its <strong>brain--</strong> files" in step2
        and 'name="folder"' not in step2
        and 'name="authkey"' not in step2
    )
    r = await post(http, "/setup/settings", step2)
    assert r.headers["location"] == "/setup?step=check"
    drop = runtime.settings.drive_dir / "inbox" / "photo.jpg"
    drop.parent.mkdir(parents=True)
    drop.write_bytes(b"x")
    step3 = (await http.get("/setup?step=check")).text
    assert "Export folder" in step3 and "data/drive/Brain" in step3
    assert "data/drive/inbox: 0 downloads waiting, 0 imported, 0 unreadable" in step3
    assert "skipped (not a .zip or folder): photo.jpg" in step3
    assert "Google" not in step3.split("<table>")[1] and "Public MCP URL" not in step3
    await post(http, "/setup/start", step3)
    assert load_config(runtime.settings.config_file) == BrainConfig(Mode.FOLDER)
    assert started == [] and "data/drive/Brain" in (await http.get("/setup")).text


@pytest.fixture
async def ready(http: httpx2.AsyncClient, runtime: Runtime) -> httpx2.AsyncClient:
    await run_wizard(http)
    await runtime.store.write_concept(
        "wiki/people/zed", person("Zed Zebrafish", private=True), "Zebrafish fan.", "test", OWNER
    )
    await runtime.store.write_concept(
        "wiki/people/amy", person("Amy Zebrafish"), "Also likes zebrafish.", "test", OWNER
    )
    return http


def bearer(**extra: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", **extra}


async def test_rest_auth(ready: httpx2.AsyncClient) -> None:
    ready.cookies.clear()
    assert (await ready.get("/search?q=zebrafish")).status_code == 401
    r = await ready.get("/search?q=zebrafish", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert (await ready.get("/search?q=zebrafish", headers=bearer())).status_code == 200


async def test_rest_hides_private_pages_from_the_token(ready: httpx2.AsyncClient) -> None:
    hits = (await ready.get("/search?q=zebrafish", headers=bearer())).json()["results"]
    assert [h["id"] for h in hits] == ["wiki/people/amy"]
    assert (await ready.get("/pages/wiki/people/zed", headers=bearer())).status_code == 404
    page = (await ready.get("/pages/wiki/people/amy", headers=bearer())).json()
    assert page["id"] == "wiki/people/amy" and page["body"].strip() == "Also likes zebrafish."
    # The admin session (the owner) sees private pages.
    hits = (await ready.get("/search?q=zebrafish")).json()["results"]
    assert {h["id"] for h in hits} == {"wiki/people/amy", "wiki/people/zed"}
    assert (await ready.get("/pages/wiki/people/zed")).json()["frontmatter"]["private"] is True


async def test_rest_put_routes_low_trust_writes_through_proposals(
    ready: httpx2.AsyncClient, runtime: Runtime
) -> None:
    payload = {
        "frontmatter": person("Bob Builder"),
        "body": "Bob builds.",
        "reason": "Met Bob",
    }
    r = await ready.put("/pages/wiki/people/bob", json=payload, headers=bearer())
    assert r.status_code == 202 and r.json()["status"] == "proposed"
    assert not runtime.store.exists(Path("wiki/people/bob.md"))  # type: ignore[arg-type]
    r = await ready.put("/pages/wiki/people/bob", json=payload)  # the owner commits
    assert r.status_code == 200 and r.json()["status"] == "committed"
    assert (await ready.get("/pages/wiki/people/bob")).json()["body"].strip() == "Bob builds."
    # write_concept's own validation applies.
    bad = await ready.put("/pages/wiki/people/bob", json={**payload, "frontmatter": {}})
    assert bad.status_code == 400 and "error" in bad.json()
    r = await ready.put("/pages/wiki/people/zed", json=payload, headers=bearer())
    assert r.status_code == 400  # a private page the token cannot see is not overwritten
    assert (
        await ready.put(
            "/pages/x", content=b"{", headers=bearer(**{"Content-Type": "application/json"})
        )
    ).status_code == 400


async def test_rest_context_pages_are_editable(ready: httpx2.AsyncClient) -> None:
    soul = (await ready.get("/pages/context/soul")).json()
    assert soul["frontmatter"]["type"] == "context"
    r = await ready.put(
        "/pages/context/soul",
        json={
            "frontmatter": soul["frontmatter"],
            "body": "# Soul\n\nBe brief.",
            "reason": "Shorter",
        },
    )
    assert r.status_code == 200
    assert (await ready.get("/pages/context/soul")).json()["body"].strip() == "# Soul\n\nBe brief."


async def test_rest_ingest(ready: httpx2.AsyncClient, runtime: Runtime) -> None:
    r = await ready.post(
        "/ingest", json={"title": "Call notes", "content": "Acme wants a demo."}, headers=bearer()
    )
    assert r.status_code == 200 and r.json()["id"].startswith("raw/")
    assert r.json()["trust"] == "external"
    hits = (await ready.get("/search?q=demo", headers=bearer())).json()["results"]
    assert hits[0]["id"] == r.json()["id"]
    r = await ready.post("/ingest", json={"title": "x", "content": "y", "trust": "owner"},
                         headers=bearer())  # fmt: skip
    assert r.status_code == 400  # the token may not claim more trust than it has
    assert (await ready.post("/ingest", json={"title": 1}, headers=bearer())).status_code == 400
    # A same-site page could send a "simple" text/plain POST with the admin cookie: refused.
    r = await ready.post(
        "/ingest",
        content=b'{"title": "Sneaky", "content": "x"}',
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 400 and "application/json" in r.json()["error"]


async def test_connect_google_from_the_wizard(runtime: Runtime) -> None:
    fake = FakeGoogle()
    google = fake.google(runtime.settings.secrets_dir, scopes=())
    (runtime.settings.secrets_dir / GOOGLE_CLIENT).unlink()  # a fresh install
    app = build_local_app(runtime, Supervisor([]), api_token=TOKEN, google=google)
    async with serve_app(app) as url, httpx2.AsyncClient(base_url=url) as http:
        http.headers["Origin"] = url
        page = (await http.get("/setup")).text
        await post(http, "/setup/password", page, password=PASSWORD, confirm=PASSWORD)
        await post(http, "/setup/mode", (await http.get("/setup")).text, mode="gdrive")
        step2 = (await http.get("/setup?step=settings")).text
        assert 'name="client_id"' in step2 and 'name="keep_key"' in step2
        assert "Connect Google" in step2 and "http://localhost:8080/setup/google/callback" in step2
        assert "needs a Workspace admin" in step2 or "Workspace admin" in step2
        client_json = json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}})
        fields = {"folder": "Company Brain", "inbox": "tasks", "prefix": "[brain]"}
        r = await http.post(
            "/setup/settings",
            data={"csrf": csrf(step2), **fields, "action": "connect"},
            files={"client_json": ("client.json", client_json, "application/json")},
        )
        assert r.status_code == 303
        consent = urlsplit(r.headers["location"])
        assert consent.netloc == "accounts.google.com"
        query = {k: v[0] for k, v in parse_qs(consent.query).items()}
        assert TASKS_SCOPE in query["scope"]
        fake.challenges["code-1"] = query["code_challenge"]

        bogus = await http.get("/setup/google/callback", params={"state": "x", "code": "code-1"})
        assert "expired or was already used" in bogus.text and google.account() is None
        done = await http.get(
            "/setup/google/callback", params={"state": query["state"], "code": "code-1"}
        )
        assert "Connected to Google as owner@acme.test" in done.text
        step2 = (await http.get("/setup?step=settings")).text
        assert "Connected as" in step2 and "Disconnect" in step2 and CLIENT_SECRET not in step2
        step3 = (await http.get("/setup?step=check")).text
        assert "folder &#34;Company Brain&#34; found or created" in step3
        assert "the inbox is readable" in step3
        await post(http, "/setup/start", step3)
    secrets = runtime.settings.secrets_dir
    assert stat.S_IMODE((secrets / GOOGLE_CLIENT).stat().st_mode) == 0o600
    assert CLIENT_SECRET not in runtime.settings.config_file.read_text()

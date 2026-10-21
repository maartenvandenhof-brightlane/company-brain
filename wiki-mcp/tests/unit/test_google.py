"""Connect Google: the OAuth flow (state, PKCE), token storage and refresh, retries."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.fake_google import CLIENT_ID, CLIENT_SECRET, REFRESH, FakeGoogle
from wiki_mcp.brain import read_secret
from wiki_mcp.google import (
    DRIVE_SCOPE,
    GOOGLE_TOKEN,
    TASKS_SCOPE,
    Google,
    GoogleError,
    parse_client,
)

REDIRECT = "http://localhost:8080/setup/google/callback"
FILES = "https://www.googleapis.com/drive/v3/files"


async def test_connect_checks_state_and_pkce_and_stores_the_token_privately(
    tmp_path: Path,
) -> None:
    fake = FakeGoogle()
    google = fake.google(tmp_path, scopes=())
    assert google.account() is None
    url = urlsplit(google.authorize_url(REDIRECT, [DRIVE_SCOPE, TASKS_SCOPE]))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com" and query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT and query["access_type"] == "offline"
    assert query["scope"].split() == ["openid", "email", DRIVE_SCOPE, TASKS_SCOPE]
    assert query["code_challenge_method"] == "S256" and CLIENT_SECRET not in url.query
    fake.challenges["code-1"] = query["code_challenge"]

    with pytest.raises(GoogleError, match="expired or was already used"):
        await google.finish("forged-state", "code-1", REDIRECT)
    assert await google.finish(query["state"], "code-1", REDIRECT) == "owner@acme.test"
    assert fake.token_forms[-1]["code_verifier"]  # only the verifier redeems the code
    with pytest.raises(GoogleError, match="already used"):  # the state is single-use
        await google.finish(query["state"], "code-1", REDIRECT)

    assert google.account() == {
        "email": "owner@acme.test",
        "scope": f"openid {DRIVE_SCOPE} {TASKS_SCOPE}",
    }
    assert google.has_scope(TASKS_SCOPE)
    path = tmp_path / GOOGLE_TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(read_secret(tmp_path, GOOGLE_TOKEN))["refresh_token"] == REFRESH

    await google.disconnect()
    assert ("POST", "/revoke") in fake.calls and google.account() is None


async def test_a_code_from_another_flow_is_refused(tmp_path: Path) -> None:
    fake = FakeGoogle()
    google = fake.google(tmp_path, scopes=())
    state = parse_qs(urlsplit(google.authorize_url(REDIRECT, [DRIVE_SCOPE])).query)["state"][0]
    fake.challenges["stolen"] = "a-challenge-of-another-verifier"
    with pytest.raises(GoogleError, match="refused"):
        await google.finish(state, "stolen", REDIRECT)
    assert google.account() is None


async def test_refreshes_on_401_and_backs_off_on_429_and_5xx(tmp_path: Path) -> None:
    fake = FakeGoogle()
    google = fake.google(tmp_path)
    fake.files["f0"] = {"name": "x", "mimeType": "application/vnd.google-apps.folder"}
    fake.queue = [401, 429, 503]
    r = await google.request("GET", f"{FILES}/f0")
    assert r.json()["id"] == "f0"
    # the first access token, then a fresh one after the 401
    assert [f["grant_type"] for f in fake.token_forms] == ["refresh_token", "refresh_token"]
    assert [c for c in fake.calls if c[0] == "sleep"] == [("sleep", "2.0"), ("sleep", "4.0")]
    fake.queue = [400]
    with pytest.raises(GoogleError) as refused:
        await google.request("GET", f"{FILES}/f0")
    assert refused.value.status == 400 and str(refused.value) == "HTTP 400: queued"


async def test_a_revoked_grant_asks_to_connect_again(tmp_path: Path) -> None:
    fake = FakeGoogle()
    google = fake.google(tmp_path)
    google.set_client("456-other.apps.googleusercontent.com", CLIENT_SECRET)
    assert google.account() is None  # a refresh token belongs to one client
    with pytest.raises(GoogleError, match="not connected"):
        await google.request("GET", f"{FILES}/f0")
    google = fake.google(tmp_path)
    (tmp_path / GOOGLE_TOKEN).write_text(json.dumps({"refresh_token": "revoked"}))
    with pytest.raises(GoogleError, match="connect again"):
        await google.request("GET", f"{FILES}/f0")


def test_parse_client() -> None:
    web = json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}})
    assert parse_client(web) == (CLIENT_ID, CLIENT_SECRET)
    for bad in ("{}", "not json", json.dumps({"web": {"client_id": "x", "client_secret": "y"}})):
        with pytest.raises(ValueError):
            parse_client(bad)
    with pytest.raises(GoogleError, match="client id"):
        Google(Path("/nonexistent")).authorize_url(REDIRECT, [DRIVE_SCOPE])

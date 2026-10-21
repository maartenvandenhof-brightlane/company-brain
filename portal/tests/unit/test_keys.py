"""Provider registry, key storage, masking and provider validation."""

from __future__ import annotations

import stat
from pathlib import Path

import httpx2
import pytest

from portal.keys import (
    KeyFormatError,
    KeyStore,
    Provider,
    check_key,
    clean_key,
    fingerprint,
    load_providers,
    mask,
)
from tests.conftest import _find

PROVIDERS = load_providers(_find("config/providers.yaml"))
ANTHROPIC, OPENAI = PROVIDERS["anthropic"], PROVIDERS["openai"]
KEY = "sk-ant-api03-" + "A" * 40 + "wxyz"


def test_registry() -> None:
    assert set(PROVIDERS) == {"anthropic", "openai", "mistral", "tavily", "jina", "exa"}
    assert ANTHROPIC.env == "ANTHROPIC_API_KEY" and ANTHROPIC.auth == "x-api-key"
    assert ANTHROPIC.headers == {"anthropic-version": "2023-06-01"}
    assert ANTHROPIC.kind == "model" and ANTHROPIC.check_body is None
    search = {n for n, p in PROVIDERS.items() if p.kind == "search"}
    assert search == {"tavily", "jina", "exa"}
    assert PROVIDERS["tavily"].models_url == "https://api.tavily.com/usage"  # its check_url
    assert all(PROVIDERS[n].purpose for n in search)


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ({"env": "PATH", "models_url": "https://x"}, "NAME_API_KEY"),
        ({"env": "X_API_KEY", "models_url": "http://x"}, "https"),
        ({"env": "X_API_KEY", "models_url": "https://x", "auth": "basic"}, "auth"),
        ({"env": "X_API_KEY", "models_url": "https://x", "kind": "other"}, "kind"),
        ({"env": "X_API_KEY", "kind": "search", "models_url": "https://x"}, "check_url"),
        (
            {"env": "X_API_KEY", "kind": "search", "check_url": "https://x", "check_body": [1]},
            "mapping",
        ),
    ],
)
def test_registry_validation(tmp_path: Path, entry: dict[str, str], needle: str) -> None:
    import yaml

    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump({"providers": {"x": entry}}))
    with pytest.raises(ValueError, match=needle):
        load_providers(f)


@pytest.mark.parametrize(
    "bad",
    ["", "short", "sk-with space-1234567890", "sk-'quoted'-1234567890", "a$(id)" * 4, "x\n" * 10],
)
def test_clean_key_rejects(bad: str) -> None:
    with pytest.raises(KeyFormatError):
        clean_key(bad)


def test_clean_key_strips() -> None:
    assert clean_key(f"  {KEY}\n") == KEY


def test_mask_and_fingerprint() -> None:
    assert mask(ANTHROPIC, KEY) == "sk-ant-…wxyz"
    assert mask(PROVIDERS["mistral"], "M" * 28 + "abcd") == "…abcd"
    assert KEY[:-4] not in mask(ANTHROPIC, KEY)
    assert len(fingerprint(KEY)) == 8 and fingerprint(KEY) not in KEY


def test_store_roundtrip(tmp_path: Path) -> None:
    store = KeyStore(tmp_path)
    (tmp_path / "keys.env").write_text("# mine\nOTHER_API_KEY=keep-me\n")
    assert store.save(ANTHROPIC, KEY, detail="ok") is False
    assert store.save(ANTHROPIC, KEY + "2", detail="ok") is True
    store.save(OPENAI, "sk-" + "b" * 30, detail="ok")
    text = (tmp_path / "keys.env").read_text()
    assert "# mine" in text and "OTHER_API_KEY=keep-me" in text
    assert text.count("ANTHROPIC_API_KEY=") == 1 and f"ANTHROPIC_API_KEY={KEY}2" in text
    for f in ("keys.env", "keys.meta.json"):
        assert stat.S_IMODE((tmp_path / f).stat().st_mode) == 0o600
    assert KEY not in (tmp_path / "keys.meta.json").read_text()

    rows = {r.provider.name: r for r in store.status(PROVIDERS)}
    assert rows["anthropic"].configured and rows["anthropic"].hint == "sk-ant-…xyz2"
    assert not rows["mistral"].configured

    assert store.remove(ANTHROPIC) == fingerprint(KEY + "2")
    assert store.remove(ANTHROPIC) is None
    assert "ANTHROPIC_API_KEY" not in (tmp_path / "keys.env").read_text()


def test_status_of_a_key_added_outside_the_portal(tmp_path: Path) -> None:
    (tmp_path / "keys.env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    row = KeyStore(tmp_path).status(PROVIDERS)[0]
    assert row.configured and row.hint == "sk-ant-…wxyz" and row.added == ""


async def test_provider_validation() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.headers.get("x-api-key") == KEY:
            return httpx2.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
        return httpx2.Response(401)

    transport = httpx2.MockTransport(handler)
    ok = await check_key(ANTHROPIC, KEY, transport=transport)
    assert ok.ok and "2 models" in ok.detail
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen[0].headers
    bad = await check_key(ANTHROPIC, "sk-ant-wrong-000000000000", transport=transport)
    assert not bad.ok and "rejected" in bad.detail

    bearer = await check_key(
        OPENAI,
        "sk-" + "c" * 30,
        transport=httpx2.MockTransport(
            lambda r: httpx2.Response(
                200 if r.headers["authorization"] == "Bearer " + "sk-" + "c" * 30 else 401, json={}
            )
        ),
    )
    assert bearer.ok

    def boom(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    down = await check_key(OPENAI, "sk-" + "c" * 30, transport=httpx2.MockTransport(boom))
    assert not down.ok and "could not reach" in down.detail
    limited = await check_key(
        OPENAI, "x" * 20, transport=httpx2.MockTransport(lambda _: httpx2.Response(429))
    )
    assert not limited.ok and "429" in limited.detail


async def test_search_key_check_posts_the_body() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        ok = request.headers.get("authorization") == "Bearer jina_" + "k" * 20
        return httpx2.Response(200 if ok else 401, json={"data": [{"id": "a"}], "results": []})

    jina = PROVIDERS["jina"]
    good = await check_key(jina, "jina_" + "k" * 20, transport=httpx2.MockTransport(handler))
    assert good.ok and good.detail == "accepted by Jina"  # no "(1 models)" for a search service
    assert seen[0].method == "POST" and seen[0].url == "https://api.jina.ai/v1/rerank"
    assert b'"documents"' in seen[0].content
    bad = await check_key(jina, "jina_" + "x" * 20, transport=httpx2.MockTransport(handler))
    assert not bad.ok and "rejected" in bad.detail

    tavily = await check_key(
        PROVIDERS["tavily"],
        "tvly-" + "t" * 20,
        transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200 if r.method == "GET" else 405, json={})
        ),
    )
    assert tavily.ok


def test_detail_never_contains_the_key() -> None:
    p = Provider("x", "X", "X_API_KEY", "x", "https://x.example/models")
    assert "secret" not in mask(p, "secret-secret-secret-1234")

"""The Drive export: one Doc per exported page, updated in place, never a private page."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

import pytest

from tests.conftest import SEED_DIR, TYPES_FILE, person
from tests.fake_google import FakeGoogle
from wiki_mcp.brain import BrainConfig, DriveSettings, Mode
from wiki_mcp.config import Settings, Trust
from wiki_mcp.gdrive import DASH, INDEX, REMOVED, START_HERE, DriveExporter, to_html
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.wiki.actor import Actor

AMY = f"Person {DASH} Amy Adams"
BOB = f"Person {DASH} Bob Brown"
OWNER = Actor("test", Trust.OWNER, frozenset({"wiki:read", "wiki:write", "wiki:private"}))
OBSERVATION = """---
type: observation
id: wiki/observations/chat-1
title: What chat 1 shows
description: Owner observation from chat 1.
private: true
---
The owner hates Mondays.
"""


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = Settings(
        wiki_dir=tmp_path / "data" / "wiki",
        state_dir=tmp_path / "data" / "state",
        data_dir=tmp_path / "data",
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,
        proposals_for="none",
    )
    rt = await create_runtime(settings, run_background=False)
    yield rt
    await rt.close()


async def started(rt: Runtime, fake: FakeGoogle) -> DriveExporter:
    exporter = DriveExporter(rt, fake.google(rt.settings.secrets_dir))
    await exporter.start(BrainConfig(Mode.GDRIVE))
    for _ in range(200):  # the first sync runs right away
        if exporter.report.get("last_sync"):
            return exporter
        await asyncio.sleep(0.01)
    raise AssertionError("no first sync")


def uploads(fake: FakeGoogle) -> list[tuple[str, str]]:
    return [c for c in fake.calls if c[1].startswith("/upload/")]


async def test_pages_become_docs_updated_in_place(runtime: Runtime) -> None:
    store = runtime.store
    await store.write_concept(
        "wiki/people/amy",
        person("Amy Adams"),
        "Amy runs [sales](/wiki/people/bob.md).",
        "test",
        OWNER,
    )
    fake = FakeGoogle()
    exporter = await started(runtime, fake)
    folders = [f for f in fake.files.values() if f["mimeType"].endswith("folder")]
    assert [f["name"] for f in folders] == ["Company Brain"]
    amy = fake.by_name(AMY)
    assert amy["mimeType"] == "application/vnd.google-apps.document"
    assert amy["html"].startswith(
        '<html><head><meta charset="utf-8"></head><body><p>Amy Adams is a person used in tests.</p>'
    )
    assert "Amy runs sales (wiki/people/bob)" in amy["html"]
    amy_id = next(k for k, f in fake.files.items() if f is amy)

    # Nothing changed: nothing is uploaded.
    fake.calls.clear()
    await exporter.sync(DriveSettings())
    assert uploads(fake) == []

    # A change overwrites the same file; nothing is ever deleted.
    await store.write_concept(
        "wiki/people/amy", person("Amy Adams"), "Amy now runs marketing.", "test", OWNER
    )
    await exporter.sync(DriveSettings())
    assert ("PATCH", f"/upload/drive/v3/files/{amy_id}") in uploads(fake)
    assert not any(m == "POST" for m, _ in uploads(fake))  # no new Doc
    assert "marketing" in fake.files[amy_id]["html"]

    # Removed from the wiki: the same Doc, titled [DEPRECATED], its text replaced.
    (store.root / "wiki/people/amy.md").unlink()
    store.reindex(PurePosixPath("wiki/people/amy.md"))
    await exporter.sync(DriveSettings())
    assert fake.files[amy_id]["name"] == "[DEPRECATED] " + AMY
    assert REMOVED in fake.files[amy_id]["html"] and "marketing" not in fake.files[amy_id]["html"]
    assert INDEX in [f["name"] for f in fake.files.values()]
    assert "Amy" not in fake.by_name(INDEX)["html"]
    fake.calls.clear()
    await exporter.sync(DriveSettings())
    assert uploads(fake) == []  # deprecated once, not again
    assert not any(m == "DELETE" for m, _ in fake.calls)
    await exporter.stop()


async def test_private_pages_and_observations_are_never_exported(runtime: Runtime) -> None:
    store = runtime.store
    await store.write_concept(
        "wiki/people/zed", person("Zed Secret", private=True), "Zed's secret.", "test", OWNER
    )
    await store.write_concept(
        "wiki/life/divorce",
        {"type": "life-event", "title": "Divorce Story", "description": "Hard times."},
        "A private life event.",
        "test",
        OWNER,
    )
    (store.root / "wiki/observations").mkdir(parents=True)
    (store.root / "wiki/observations/chat-1.md").write_text(OBSERVATION)
    store.reindex(PurePosixPath("wiki/observations/chat-1.md"))
    await store.ingest_source("Raw notes", "Raw text", None, "owner", OWNER)
    await store.write_concept("wiki/people/amy", person("Amy Adams"), "Public.", "test", OWNER)

    fake = FakeGoogle()
    exporter = await started(runtime, fake)
    everything = " ".join(f"{f['name']} {f.get('html', '')}" for f in fake.files.values())
    assert "Amy Adams" in everything
    for hidden in ("Zed", "Divorce", "Hard times", "Mondays", "chat 1", "Raw notes"):
        assert hidden not in everything, hidden

    # A page that turns private loses its exported text.
    await store.set_private("wiki/people/amy", True, "test", OWNER)
    await exporter.sync(DriveSettings())
    amy = [f for f in fake.docs().values() if "Amy Adams" in f["name"]]
    assert [f["name"] for f in amy] == ["[DEPRECATED] " + AMY]
    assert "Public." not in amy[0]["html"]
    await exporter.stop()


async def test_start_here_and_index(runtime: Runtime) -> None:
    store = runtime.store
    await store.write_concept("wiki/people/amy", person("Amy Adams"), "Hi.", "test", OWNER)
    fake = FakeGoogle()
    exporter = await started(runtime, fake)
    start = fake.by_name(START_HERE)["html"]
    assert "Google Task titled &quot;[brain] &lt;short title&gt;&quot;" in start
    assert INDEX in start
    assert "shared memory. Answer from the wiki first" in start  # context/soul feeds it
    index = fake.by_name(INDEX)["html"]
    assert f"<b>{AMY}</b> {DASH} Amy Adams is a person used in tests." in index

    await exporter.sync(DriveSettings(inbox="keep", prefix="[kb]"))
    start = fake.by_name(START_HERE)["html"]
    assert "Google Keep note titled &quot;[kb] &lt;short title&gt;&quot;" in start
    await exporter.stop()


async def test_a_failing_doc_does_not_stop_the_run(runtime: Runtime) -> None:
    store = runtime.store
    await store.write_concept("wiki/people/amy", person("Amy Adams"), "Hi.", "test", OWNER)
    await store.write_concept("wiki/people/bob", person("Bob Brown"), "Hi.", "test", OWNER)
    fake = FakeGoogle()
    fake.fail[AMY] = 400
    exporter = await started(runtime, fake)
    assert fake.by_name(BOB)
    assert exporter.report["errors"] == ["wiki/people/amy: HTTP 400: no"]
    del fake.fail[AMY]
    await exporter.sync(DriveSettings())
    assert fake.by_name(AMY) and exporter.report["errors"] == []
    await exporter.stop()


def test_markdown_becomes_text_only_html() -> None:
    out = to_html(
        "See ![the org chart](/assets/org.png) and [Bob](../people/bob.md), "
        "[docs](https://example.com/x) and <https://example.com/y>.\n\n<script>alert(1)</script>",
        PurePosixPath("wiki/projects/p.md"),
    )
    assert "<img" not in out and "the org chart" in out
    assert "Bob (wiki/people/bob)" in out and "docs (https://example.com/x)" in out
    assert "https://example.com/y." in out and "(https://example.com/y)" not in out
    assert "<a " not in out and "<script>" not in out and "&lt;script&gt;" in out

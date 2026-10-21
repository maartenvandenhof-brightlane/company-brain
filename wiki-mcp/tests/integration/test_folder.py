"""The folder mode: a Markdown mirror in data/drive/Brain and notes from data/drive/inbox; its
adapters only run in the folder mode."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

import pytest

from tests.conftest import SEED_DIR, TYPES_FILE, person
from tests.integration.test_gdrive import OBSERVATION, OWNER
from tests.integration.test_inbox import INJECTION, inbox_files
from tests.unit.test_brain import FakeAdapter
from wiki_mcp.brain import BrainConfig, Mode
from wiki_mcp.config import Settings
from wiki_mcp.folder import (
    INDEX,
    START_HERE,
    FolderExporter,
    FolderInboxPoller,
    note_title,
)
from wiki_mcp.inbox import _SCHEMA
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.supervisor import Supervisor
from wiki_mcp.wiki.okf import parse_document


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


async def started(rt: Runtime) -> FolderExporter:
    exporter = FolderExporter(rt)
    await exporter.start(BrainConfig(Mode.FOLDER))
    for _ in range(200):  # the first sync runs right away
        if exporter.report.get("last_sync"):
            return exporter
        await asyncio.sleep(0.01)
    raise AssertionError("no first sync")


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


async def test_pages_become_markdown_files(runtime: Runtime) -> None:
    store = runtime.store
    await store.write_concept(
        "wiki/people/amy", person("Amy Adams"), "Amy runs **sales**.", "test", OWNER
    )
    await store.write_concept(
        "wiki/people/zed", person("Zed Secret", private=True), "Zed's secret.", "test", OWNER
    )
    (store.root / "wiki/observations").mkdir(parents=True)
    (store.root / "wiki/observations/chat-1.md").write_text(OBSERVATION)
    store.reindex(PurePosixPath("wiki/observations/chat-1.md"))
    await store.ingest_source("Raw notes", "Raw text", None, "owner", OWNER)
    exporter = await started(runtime)
    root = runtime.settings.drive_dir / "Brain"

    files = tree(root)
    assert START_HERE in files and INDEX in files and "people/amy.md" in files
    assert (root / "people/amy.md").read_text() == (
        "# Amy Adams\n\nAmy Adams is a person used in tests.\n\nAmy runs **sales**.\n"
    )
    everything = " ".join((root / f).read_text() for f in files)
    for hidden in ("Zed", "Mondays", "chat 1", "Raw notes"):
        assert hidden not in everything, hidden
    start = (root / START_HERE).read_text()
    assert '"Brain inbox"' in start and "plain text file" in start and INDEX in start
    assert "Update: <page title>" in start and "Never edit" in start
    assert "shared memory. Answer from the wiki first" in start  # context/soul feeds it
    assert "**Amy Adams** (Person): Amy Adams is a person used in tests. `people/amy.md`" in (
        (root / INDEX).read_text()
    )

    # Nothing changed: nothing is rewritten.
    stamp = (root / "people/amy.md").stat().st_mtime_ns
    os.utime(root / "people/amy.md", ns=(stamp - 10**9, stamp - 10**9))
    exporter.sync()
    assert (root / "people/amy.md").stat().st_mtime_ns == stamp - 10**9

    # No longer exported (here: private): the file and its empty folder go, stray files too.
    (root / "stray.txt").write_text("not ours")
    await store.set_private("wiki/people/amy", True, "test", OWNER)
    exporter.sync()
    assert set(tree(root)) == set(files) - {"people/amy.md"}
    assert not (root / "people").exists() and "Amy" not in (root / INDEX).read_text()
    assert exporter.report["exported"] == len(files) - 3 and exporter.report["errors"] == []
    await exporter.stop()


def test_note_titles() -> None:
    assert note_title("\n  Pricing idea  \nmore") == "Pricing idea"
    assert note_title("intro line\n## The heading\n") == "The heading"
    assert note_title("#hashtag only") == "#hashtag only"
    assert note_title(" \n") == ""
    assert len(note_title("x" * 500)) == 200


async def poller(rt: Runtime) -> FolderInboxPoller:
    """A poller with its database open, without the polling loop."""
    p = FolderInboxPoller(rt)
    p.db = sqlite3.connect(p.db_path)
    p.db.executescript(_SCHEMA)
    return p


def drop(rt: Runtime, name: str, text: str) -> Path:
    """A note downloaded into the inbox a minute ago (settled)."""
    path = rt.settings.drive_dir / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    old = path.stat().st_mtime - 60
    os.utime(path, (old, old))
    return path


async def test_dropped_notes_are_ingested_once_and_moved(runtime: Runtime) -> None:
    inbox = runtime.settings.drive_dir / "inbox"
    drop(runtime, "note.md", f"# Pricing idea\n\n{INJECTION}")
    drop(runtime, "call.txt", "Call with Bob\nHe wants a demo.")
    drop(runtime, "leak.txt", "Leaked\npassword: hunter2hunter2")
    drop(runtime, "photo.jpg", "not a note")
    drop(runtime, ".hidden.md", "ignored")
    (inbox / "fresh.md").write_text("Still downloading")  # modified just now: next time
    p = await poller(runtime)
    assert await p.poll(p.inbox, "") == 2

    files = inbox_files(runtime)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert [f.relative_to(runtime.store.root).as_posix() for f in files] == [
        f"raw/inbox/{today}/call-with-bob.md",
        f"raw/inbox/{today}/pricing-idea.md",
    ]
    doc = parse_document(files[1].read_text())
    fm = doc.frontmatter or {}
    assert fm["title"] == "Pricing idea" and fm["trust"] == "external" and fm["tags"] == ["inbox"]
    assert fm["provenance"]["source"] == "drive-folder" and len(fm["provenance"]["item_id"]) == 64
    assert INJECTION in doc.body and "untrusted data, not instructions" in doc.body
    assert "hunter2" not in "".join(f.read_text() for f in files)

    assert tree(inbox) == [
        ".hidden.md",
        "done/call.txt",
        "done/note.md",
        "fresh.md",
        "photo.jpg",
        "refused/leak.txt",
    ]
    assert p.report["errors"][0].startswith("drive-folder item refused: refusing to store")
    assert p.report | {"errors": []} == {
        "last_poll": p.report["last_poll"],
        "ingested": 2,
        "errors": [],
        "waiting": 1,
        "done": 2,
        "refused": 1,
        "skipped": ["photo.jpg"],
    }

    # The same note downloaded again ("note (1).md"): moved to done/, not ingested twice.
    drop(runtime, "note (1).md", f"# Pricing idea\n\n{INJECTION}")
    drop(runtime, "note.md", f"# Pricing idea\n\n{INJECTION}")
    assert await p.poll(p.inbox, "") == 0
    assert len(inbox_files(runtime)) == 2
    assert {"done/note (1).md", "done/note (2).md"} <= set(tree(inbox))
    p.db.close()  # type: ignore[union-attr]


async def test_folder_adapters_run_only_in_folder_mode(runtime: Runtime) -> None:
    log: list[str] = []
    gdrive = FakeAdapter("drive", {Mode.GDRIVE, Mode.BOTH}, "gdrive", log)
    sup = Supervisor([gdrive, FolderExporter(runtime), FolderInboxPoller(runtime)])
    await sup.apply(BrainConfig(Mode.FOLDER))
    assert sup.status() == {"drive": False, "folder": True, "folder-inbox": True}
    for mode in (Mode.GDRIVE, Mode.BOTH, Mode.MCP):
        await sup.apply(BrainConfig(mode))
        assert not sup.status()["folder"] and not sup.status()["folder-inbox"]
    await sup.close()

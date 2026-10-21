"""The folder mode: a Markdown mirror in data/drive/Brain and the pages Gemini wrote into it,
downloaded back into data/drive/inbox; its adapters only run in the folder mode."""

from __future__ import annotations

import asyncio
import os
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

import pytest

from tests.conftest import SEED_DIR, TYPES_FILE, person
from tests.integration.test_gdrive import OBSERVATION, OWNER
from tests.unit.test_brain import FakeAdapter
from wiki_mcp.brain import BrainConfig, Mode
from wiki_mcp.config import Settings
from wiki_mcp.folder import (
    INDEX,
    START_HERE,
    FolderExporter,
    FolderInboxPoller,
    digest,
    parse_page,
)
from wiki_mcp.runtime import Runtime, create_runtime
from wiki_mcp.supervisor import Supervisor


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = Settings(
        wiki_dir=tmp_path / "data" / "wiki",
        state_dir=tmp_path / "data" / "state",
        data_dir=tmp_path / "data",
        types_file=TYPES_FILE,
        seed_dir=SEED_DIR,  # proposals for external writes, the default: the import is agent
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
    assert "inbox" not in start and "plain text file" in start and INDEX in start
    assert "`<name> v2.md`" in start and "highest version" in start and "Never edit" in start
    assert "(`people`, `organizations`" in start and "`life`" not in start  # private by default
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


def test_parse_page() -> None:
    assert parse_page("# Amy\r\n\r\nWho Amy is.\r\n\r\nBody\r\n## More") == (
        "Amy",
        "Who Amy is.",
        "Body\n## More\n",
    )
    assert parse_page("# Amy\n## Heading\nBody") == ("Amy", "", "## Heading\nBody\n")
    with pytest.raises(ValueError, match="first line"):
        parse_page("Amy\n\nBody")
    assert digest("a\r\nb\r\n") == digest("a\nb") != digest("a\n\nb")


def settled(path: Path) -> Path:
    old = path.stat().st_mtime - 60
    for f in [path, *path.rglob("*")]:
        os.utime(f, (old, old))
    return path


def drop_zip(rt: Runtime, name: str, files: dict[str, str]) -> Path:
    """A download of the Drive folder, a minute old (settled)."""
    path = rt.settings.drive_dir / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for member, text in files.items():
            z.writestr(member, text)
    return settled(path)


async def test_a_downloaded_zip_brings_back_new_pages_and_versions(runtime: Runtime) -> None:
    store = runtime.store
    await store.write_concept(
        "wiki/people/amy", person("Amy Adams", aliases=["Amy"]), "Amy runs sales.", "test", OWNER
    )
    await store.write_concept(
        "wiki/people/zed", person("Zed Secret", private=True), "Zed's secret.", "test", OWNER
    )
    exporter = await started(runtime)
    await exporter.stop()
    root = runtime.settings.drive_dir / "Brain"
    # Drive gives the export back with other line endings and without the last newline.
    exported = {
        f"Brain/{f}": (root / f).read_text().replace("\n", "\r\n").rstrip() for f in tree(root)
    }
    page = "# {}\n\n{} is a person used in tests.\n\n{}\n"
    download = exported | {
        "Brain/people/amy v2.md": page.format("Amy Adams", "Amy Adams", "Amy ran sales."),
        "Brain/people/amy V3 (1).md": page.format("Amy B. Adams", "Amy", "Amy runs marketing."),
        "Brain/people/bob.md": page.format("Bob Baker", "Bob", "Bob builds things."),
        "Brain/people/Carol Clark.md": "# Carol Clark\n\n## Role\n\nCarol runs ops.\n",
        "Brain/people/zed v2.md": page.format("Zed", "Zed", "Not private any more."),
        "Brain/nowhere/thing.md": page.format("Thing", "Thing", "Lost."),
        "Brain/people/leak.md": page.format("Leak", "Leak", "password: hunter2hunter2"),
        "Brain/people/notes.txt": "not a page",
    }
    drop_zip(runtime, "Brain-20261021.zip", download)
    p = FolderInboxPoller(runtime)
    await p.poll()

    assert p.report["last_import"] == {
        "imported": ["Brain-20261021.zip"],
        "created": ["people/bob.md", "people/carol-clark.md"],
        "updated": ["people/amy.md"],
        "proposed": [],
        "skipped": len(exported) - 3,  # the other exported pages; not amy: she has versions
        "refused": [
            "nowhere/thing.md: no kind of page lives in nowhere/",
            p.report["last_import"]["refused"][1],
            "people/zed v2.md: the brain does not share this page with Drive",
        ],
    }
    assert (
        "people/leak.md: refusing to store what looks like a credential"
        in (p.report["last_import"]["refused"][1])
    )
    amy = store.read("wiki/people/amy")[1]
    assert amy.frontmatter is not None
    assert amy.frontmatter["title"] == "Amy B. Adams" and amy.frontmatter["aliases"] == ["Amy"]
    assert amy.frontmatter["description"] == "Amy is a person used in tests."
    assert amy.body.strip() == "Amy runs marketing."
    bob = store.read("wiki/people/bob")[1].frontmatter or {}
    assert bob["type"] == "person" and bob["generated"]["by"] == "process:drive-folder"
    carol = store.read("wiki/people/carol-clark")[1].frontmatter or {}
    assert carol["title"] == carol["description"] == "Carol Clark"  # no description line
    assert "Not private" not in store.read("wiki/people/zed")[1].body
    assert not store.exists(PurePosixPath("wiki/people/leak.md"))
    assert store.repo.log(limit=1)[0].body.startswith("Gemini via Google Drive: people/")
    inbox = runtime.settings.drive_dir / "inbox"
    assert tree(inbox) == ["done/Brain-20261021.zip"]
    assert p.report | {"last_import": {}, "errors": []} == {
        "last_poll": p.report["last_poll"],
        "last_import": {},
        "errors": [],
        "waiting": 0,
        "done": 1,
        "refused": 0,
        "skipped": [],
    }

    # The same download again: nothing changes, also not by the older v2.
    await store.write_concept(
        "wiki/people/amy", person("Amy Adams"), "Amy runs sales again.", "test", OWNER
    )
    drop_zip(runtime, "Brain-20261021.zip", download)
    await p.poll()
    assert p.report["last_import"]["skipped"] == len(exported) - 3 + 3  # and amy, bob and carol
    assert p.report["last_import"]["created"] == p.report["last_import"]["updated"] == []
    assert store.read("wiki/people/amy")[1].body.strip() == "Amy runs sales again."
    assert tree(inbox) == ["done/Brain-20261021 (2).zip", "done/Brain-20261021.zip"]


async def test_an_unzipped_folder_is_imported_too(runtime: Runtime) -> None:
    inbox = runtime.settings.drive_dir / "inbox"
    folder = inbox / "Brain" / "concepts"
    folder.mkdir(parents=True)
    (folder / "pricing.md").write_text("# Pricing\n\nHow we price.\n\nPer seat.\n")
    (inbox / "photo.jpg").write_bytes(b"x")
    (inbox / ".DS_Store").write_bytes(b"x")
    p = FolderInboxPoller(runtime)
    await p.poll()  # still being copied: next time
    assert "last_import" not in p.report and p.report["waiting"] == 1
    settled(inbox / "Brain")
    await p.poll()
    assert p.report["last_import"]["created"] == ["concepts/pricing.md"]
    fm = runtime.store.read("wiki/concepts/pricing")[1].frontmatter or {}
    assert fm["type"] == "concept" and fm["description"] == "How we price."
    assert tree(inbox) == [".DS_Store", "done/Brain/concepts/pricing.md", "photo.jpg"]
    assert p.report["skipped"] == ["photo.jpg"]


async def test_an_unsafe_zip_is_refused_whole(runtime: Runtime) -> None:
    drop_zip(runtime, "evil.zip", {"Brain/../../wiki/x.md": "# X\n\nX.\n\nX\n"})
    broken = runtime.settings.drive_dir / "inbox" / "broken.zip"
    broken.write_bytes(b"not a zip")
    settled(broken)
    p = FolderInboxPoller(runtime)
    await p.poll()
    assert p.report["errors"] == [
        "broken.zip not imported: File is not a zip file",
        "evil.zip not imported: unsafe file names in the zip",
    ]
    assert tree(runtime.settings.drive_dir / "inbox") == ["refused/broken.zip", "refused/evil.zip"]
    assert p.report["refused"] == 2 and p.report["last_import"]["imported"] == []


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

"""Project consolidation preserves evidence and requires the exact reviewed snapshots."""

from pathlib import PurePosixPath

import pytest

from wiki_mcp.portal_api import OWNER_ACTOR
from wiki_mcp.portal_reads import docs_body, graph_body, index_body
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.okf import parse_document, serialize_document
from wiki_mcp.wiki.store import WikiError, WikiStore

DUP = "wiki/projects/ai-agentic-system"
CANON = "wiki/projects/second-brain-vault"


async def seed(store: WikiStore, owner: Actor) -> tuple[str, str]:
    await store.write_concept(
        "wiki/concepts/plan",
        {
            "type": "concept",
            "title": "Plan",
            "description": "Project plan.",
        },
        "The plan.",
        "Seed plan",
        owner,
    )
    common = "- 2026-09-01: Owner approved the project."
    canonical = """# Second brain vault

## Facts

- 2026-09-01: Owner approved the project.
- Storage is SQLite. [^evidence]

## Timeline

- 2026-09-02: Started implementation.

[^evidence]: https://example.com/sqlite
"""
    duplicate = f"""# AI agentic system

## Facts

{common}
- Storage is PostgreSQL. [^evidence]
- A distinct claim with a different citation. [^extra]

## Timeline

- 2026-09-03: Discussed the architecture. [^extra]

## Decisions

- 2026-09-04: Keep an audit trail. [^extra]
  This continuation explains why.

## Follow-ups

- [ ] 2026-09-05: Check the deployment.

## Design

The full prose must survive.

### Example

```python
# not a section
print("keep this")
```

[Plan](../concepts/plan.md)

[^evidence]: https://example.com/postgres
[^extra]: https://example.com/meeting
"""
    for pid, title, body, aliases, resource in (
        (CANON, "Second brain vault", canonical, ["Vault"], "https://example.com/sqlite"),
        (DUP, "AI agentic system", duplicate, ["Agent system"], "https://example.com/postgres"),
    ):
        await store.write_concept(
            pid,
            {
                "type": "project",
                "title": title,
                "description": title + " project.",
                "aliases": aliases,
                "related": ["wiki/concepts/plan"],
                "sources": [{"id": "evidence", "resource": resource}],
            },
            body,
            "Seed project",
            owner,
        )
    return store.read(DUP)[2], store.read(CANON)[2]


async def propose(store: WikiStore, owner: Actor) -> str:
    result = await store.propose_identity_merge(DUP, CANON, "Same project", owner)
    assert result.proposal_id
    return result.proposal_id


def page(store: WikiStore, pid: str):
    return store.read(pid)[1]


async def test_full_project_content_citations_conflicts_and_history(store: WikiStore, owner: Actor):
    before_dup, before_canon = await seed(store, owner)
    original_sha = store.repo.head()
    pid = await propose(store, owner)
    proposal = page(store, pid).frontmatter["payload"]
    preview = parse_document(proposal["preview"])
    assert "Storage is SQLite" in preview.body and "Storage is PostgreSQL" in preview.body
    assert preview.body.count("Owner approved the project.") == 1
    assert "unresolved" in preview.body
    assert (
        preview.frontmatter["merge_history"][0]["metadata_conflicts"][0]["field"] == "description"
    )
    assert store.read(DUP)[2] == before_dup and store.read(CANON)[2] == before_canon
    result = await store.accept_proposal(
        pid, "Confirmed", OWNER_ACTOR, review_token=proposal["review_token"]
    )
    assert result.status == "committed"
    body = page(store, CANON).body
    for text in (
        "2026-09-03",
        "2026-09-04",
        "2026-09-05",
        "This continuation",
        "full prose",
        "# not a section",
        'print("keep this")',
        "/wiki/concepts/plan.md",
        "[^extra]: https://example.com/meeting",
    ):
        assert text in body
    assert "[^evidence]: https://example.com/sqlite" in body
    assert "[^evidence-merged-" in body
    fm = page(store, CANON).frontmatter
    assert set(fm["aliases"]) == {"Vault", "AI agentic system", "Agent system"}
    assert {s["resource"] for s in fm["sources"]} == {
        "https://example.com/sqlite",
        "https://example.com/postgres",
    }
    renamed = next(s["id"] for s in fm["sources"] if "postgres" in s["resource"])
    assert f"Storage is PostgreSQL. [^{renamed}]" in body
    assert f"[^{renamed}]: https://example.com/postgres" in body
    assert fm["related"] == ["wiki/concepts/plan"]
    original = parse_document(before_dup).frontmatter
    assert fm["merge_history"][0]["frontmatter"] == original
    assert store.repo._run("show", f"{original_sha}:{DUP}.md").stdout == before_dup
    assert store.repo._run("show", f"{original_sha}:{CANON}.md").stdout == before_canon
    redirect = page(store, DUP)
    assert redirect.frontmatter["canonical"] == CANON
    assert "PostgreSQL" not in redirect.body and len(redirect.body) < 150
    assert store.read_redirected(DUP, owner)[0] == PurePosixPath(CANON + ".md")
    head = store.repo.head()
    again = await store.accept_proposal(
        pid, "Retry", OWNER_ACTOR, review_token=proposal["review_token"]
    )
    assert again.status == "unchanged" and store.repo.head() == head
    assert body == page(store, CANON).body
    assert store.list_proposals() == []


@pytest.mark.parametrize("changed", [DUP, CANON])
async def test_changed_snapshot_requires_renewed_review(
    store: WikiStore, owner: Actor, changed: str
):
    await seed(store, owner)
    pid = await propose(store, owner)
    old_token = page(store, pid).frontmatter["payload"]["review_token"]
    await store.append_to_concept(changed, "Facts", "- A new fact.", "Changed page", owner)
    before = store.read(DUP)[2], store.read(CANON)[2]
    with pytest.raises(WikiError, match="page changed"):
        await store.accept_proposal(pid, "Confirm", OWNER_ACTOR, review_token=old_token)
    assert before == (store.read(DUP)[2], store.read(CANON)[2])
    await store.refresh_identity_preview(pid, OWNER_ACTOR)
    with pytest.raises(WikiError, match="preview changed"):
        await store.accept_proposal(pid, "Old tab", OWNER_ACTOR, review_token=old_token)
    new = page(store, pid).frontmatter["payload"]
    await store.accept_proposal(pid, "New review", OWNER_ACTOR, review_token=new["review_token"])
    assert "A new fact" in page(store, CANON).body


async def test_visibility_history_and_graph_links(store: WikiStore, owner: Actor):
    await seed(store, owner)
    await store.write_concept(
        "wiki/concepts/link",
        {
            "type": "concept",
            "title": "Link",
            "description": "Links to project.",
        },
        f"[Project](/{DUP}.md)",
        "Seed",
        owner,
    )
    pid = await propose(store, owner)
    count = len(index_body(store)["docs"])
    await store.accept_proposal(pid, "Confirm", OWNER_ACTOR)
    index = index_body(store)
    assert len(index["docs"]) == count - 1
    assert DUP in index["known_ids"]
    assert DUP not in {d["id"] for d in index["docs"]}
    assert DUP not in {d["id"] for d in docs_body(store, type="project")["docs"]}
    assert DUP in {d["id"] for d in docs_body(store, include_merged=True)["docs"]}
    assert DUP not in {h.row.id for h in store.index.search("AI agentic system")}
    assert CANON in {h.row.id for h in store.index.search("PostgreSQL")}
    graph = graph_body(store, set())
    assert DUP not in {n["id"] for n in graph["nodes"]}
    assert {"source": "wiki/concepts/link", "target": CANON} in graph["edges"]
    assert any(loop["page"] == CANON for loop in store.open_loops())
    assert all(loop["page"] != DUP for loop in store.open_loops())


async def test_explicit_legacy_migration(store: WikiStore, owner: Actor):
    await seed(store, owner)
    rel = PurePosixPath(DUP + ".md")
    doc = page(store, DUP)
    fm = {**doc.frontmatter, "canonical": CANON, "status": "deprecated"}
    # Simulate the old accepted merge, committed to history but with its body still separate.
    store._write_file(rel, serialize_document(fm, doc.body))
    store.repo.commit([rel], "Legacy redirect-only merge", author_name="Test", author_email="t@x")
    store.reindex(rel)
    with pytest.raises(WikiError, match="merged into"):
        await propose(store, owner)
    result = await store.propose_identity_merge(
        DUP, CANON, "Review old content", owner, consolidate_merged=True
    )
    assert result.proposal_id
    assert "full prose" not in page(store, CANON).body
    await store.accept_proposal(result.proposal_id, "Review completed", OWNER_ACTOR)
    assert "full prose" in page(store, CANON).body
    with pytest.raises(WikiError, match="merged into"):
        await store.propose_identity_merge(DUP, CANON, "Repeat", owner, consolidate_merged=True)


async def test_atomic_failure_restores_every_file(store: WikiStore, owner: Actor, monkeypatch):
    await seed(store, owner)
    pid = await propose(store, owner)
    before = {p: store.read(p)[2] for p in (DUP, CANON, pid)}
    head = store.repo.head()
    real_commit = store.repo.commit

    def fail(paths, *args, **kwargs):
        store.repo._run("add", "-A", "--", *(str(p) for p in paths))
        raise RuntimeError("Injected commit failure")

    monkeypatch.setattr(store.repo, "commit", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        await store.accept_proposal(pid, "Confirm", OWNER_ACTOR)
    assert {p: store.read(p)[2] for p in before} == before
    assert store.repo.head() == head and not store.repo.is_dirty()
    assert not (store.root / "merge-receipts" / (pid.split("/")[-1] + ".md")).exists()
    monkeypatch.setattr(store.repo, "commit", real_commit)
    await store.accept_proposal(pid, "Retry after failure", OWNER_ACTOR)


async def test_concurrent_approval_and_later_edits_keep_provenance(store: WikiStore, owner: Actor):
    import asyncio

    await seed(store, owner)
    pid = await propose(store, owner)
    results = await asyncio.gather(
        store.accept_proposal(pid, "First approval", OWNER_ACTOR),
        store.accept_proposal(pid, "Concurrent retry", OWNER_ACTOR),
    )
    assert {r.status for r in results} == {"committed", "unchanged"}
    history = page(store, CANON).frontmatter["merge_history"]
    assert len(history) == 1
    await store.append_to_concept(CANON, "Facts", "- A later fact.", "Later edit", owner)
    assert page(store, CANON).frontmatter["merge_history"] == history


def test_textual_multiline_citations_and_literal_code():
    from wiki_mcp.wiki.consolidate import consolidate
    from wiki_mcp.wiki.okf import Document

    canonical = Document(
        {"title": "Second brain vault"},
        """# Second brain vault

## Facts

- One claim. [^note]

[^note]: Meeting record.
    First account.
""",
    )
    duplicate = Document(
        {"title": "AI agentic system"},
        """# AI agentic system

## Facts

- One claim. [^note]

[^note]: Meeting record.
    Second account.

## Code

`[Example](../example.md) [^note]`

```markdown
[Example](../example.md) [^note]
```
""",
    )
    merged = parse_document(consolidate(duplicate, canonical, DUP, CANON, {}))
    assert merged.body.count("- One claim.") == 2  # distinct evidence, identical claim words
    assert "Meeting record." in merged.body and "/wiki/projects/Meeting" not in merged.body
    assert "First account." in merged.body and "Second account." in merged.body
    assert "[^note-merged-" in merged.body
    assert merged.body.count("[Example](../example.md) [^note]") == 2


async def test_uncommitted_original_cannot_be_lost(store: WikiStore, owner: Actor):
    await seed(store, owner)
    rel = PurePosixPath(DUP + ".md")
    text = store.read(DUP)[2] + "\nUncommitted observation.\n"
    store._write_file(rel, text)
    pid = await propose(store, owner)
    with pytest.raises(WikiError, match="commit page edits"):
        await store.accept_proposal(pid, "Confirm", OWNER_ACTOR)
    assert store.read(DUP)[2] == text


async def test_preview_and_provenance_do_not_expose_private_source_ids(
    store: WikiStore, owner: Actor
):
    await seed(store, owner)
    private_owner = OWNER_ACTOR
    source = await store.ingest_source(
        "Private evidence", "Private transcript.", None, "agent", private_owner
    )
    await store.set_private(source.id, True, "Sensitive evidence", private_owner)
    rel, doc, _ = store.read(DUP)
    fm = {
        **doc.frontmatter,
        "sources": [*doc.frontmatter["sources"], {"id": "secret", "resource": "/" + source.path}],
    }
    store._write_file(rel, serialize_document(fm, doc.body))
    store.repo.commit([rel], "Source reference", author_name="Test", author_email="t@x")
    store.reindex(rel)
    pid = await propose(store, owner)
    assert store.list_proposals(private=False) == []
    await store.accept_proposal(pid, "Reviewed privately", private_owner)
    assert source.id in str(page(store, CANON).frontmatter["merge_history"])
    visible = store.read_visible(CANON, owner)[1]
    assert source.id not in str(visible.frontmatter)

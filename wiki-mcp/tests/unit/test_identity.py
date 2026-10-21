"""Identities: `aliases`, duplicates redirecting to their canonical page (`canonical:`), the
merge_identity proposal only the owner accepts in the portal, and no person page for the owner."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from tests.conftest import person
from tests.unit.test_private import Tools
from wiki_mcp.config import Trust
from wiki_mcp.portal_api import OWNER_ACTOR
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.okf import alias_from_title, parse_document
from wiki_mcp.wiki.store import FilingPage, FilingSection, WikiError, WikiStore

OWNER = "wiki/owner/profile"
DUP = "wiki/people/maarten-van-den-hof"
FRIEND = "wiki/people/jane-doe"
NOTE = "wiki/concepts/garden"
DUP_BODY = "# Maarten van den Hof\n\n- 2026-09: Lives in Ghent.\n"


@pytest.fixture
def me() -> Actor:
    return Actor("chatgpt", Trust.AGENT, frozenset({"wiki:read", "wiki:write", "wiki:private"}))


@pytest.fixture
def guest() -> Actor:
    """No wiki:private, but admin: it may accept other proposals."""
    return Actor("librechat", Trust.AGENT, frozenset({"wiki:read", "wiki:write", "wiki:admin"}))


@pytest.fixture
def tools(store: WikiStore) -> Tools:
    return Tools(store)


async def _seed(store: WikiStore, me: Actor) -> None:
    owner = {"type": "owner", "title": "Owner profile", "description": "The owner."}
    await store.write_concept(OWNER, owner, "# Owner profile\n\n## Background", "test", me)
    await store.write_concept(DUP, person("Maarten van den Hof"), DUP_BODY, "import", me)
    await store.write_concept(
        FRIEND, person(), "Knows [Maarten](/wiki/people/maarten-van-den-hof.md).", "test", me
    )
    garden = {"type": "concept", "title": "Garden", "description": "A garden.", "related": [DUP]}
    await store.write_concept(NOTE, garden, "Planted by the owner.", "test", me)


async def _merged(store: WikiStore, tools: Tools, me: Actor) -> str:
    await _seed(store, me)
    proposed = await tools.ok(me, "merge_identity", duplicate=DUP, canonical=OWNER, reason="Same")
    await store.accept_proposal(proposed["proposal_id"], "Yes, that is me", OWNER_ACTOR)
    return str(proposed["proposal_id"])


def _fm(store: WikiStore, did: str) -> dict[str, object]:
    return parse_document(store.read_text_file(did + ".md")).frontmatter or {}


# ------------------------------------------------------------------ aliases


async def test_aliases_are_validated_sticky_and_searchable(store: WikiStore, me: Actor) -> None:
    fm = person("Jane Doe", aliases=["  Janie  ", "janie", "J. Doe"])
    await store.write_concept(FRIEND, fm, "A friend.", "test", me)
    assert _fm(store, FRIEND)["aliases"] == ["Janie", "J. Doe"]  # normalised, deduplicated
    [hit] = store.index.search("janie", private=False)
    assert hit.row.id == FRIEND and hit.row.aliases == ["Janie", "J. Doe"]
    # A rewrite that does not mention them keeps them; `canonical` is never taken from a write.
    await store.write_concept(FRIEND, person("Jane Doe", canonical=OWNER), "Again.", "test", me)
    assert _fm(store, FRIEND)["aliases"] == ["Janie", "J. Doe"]
    assert "canonical" not in _fm(store, FRIEND)
    for bad in ([""], ["x" * 201], [1], [f"n{i}" for i in range(21)], {"a": 1}):
        with pytest.raises(WikiError, match="alias"):
            await store.write_concept(FRIEND, person(aliases=bad), "Bad.", "test", me)


# ------------------------------------------------------------------ merge_identity


async def test_merge_identity_only_proposes(
    store: WikiStore, tools: Tools, me: Actor, owner: Actor
) -> None:
    await _seed(store, me)
    before = store.read_text_file(DUP + ".md")
    # Whatever the caller's trust: a proposal, nothing changed.
    result = await tools.ok(
        owner, "merge_identity", duplicate=DUP, canonical=OWNER, reason="Same person"
    )
    assert result["status"] == "proposed" and "portal" in result["message"]
    assert store.read_text_file(DUP + ".md") == before
    [listed] = (await tools.ok(me, "review_proposals"))["proposals"]
    assert listed["action"] == "identity" and listed["target"] == DUP + ".md"
    assert (listed["duplicate"], listed["canonical"]) == (DUP, OWNER)
    assert (listed["duplicate_title"], listed["canonical_title"]) == (
        "Maarten van den Hof",
        "Owner profile",
    )
    body = store.read_text_file(listed["proposal_id"] + ".md")
    assert f"`{DUP}` gets `canonical: {OWNER}`" in body
    # Proposed once.
    again = await tools.error(
        me, "merge_identity", duplicate=DUP, canonical=OWNER, reason="Same person"
    )
    assert "proposed already" in again


async def test_only_the_owner_accepts_in_the_portal(
    store: WikiStore, tools: Tools, me: Actor, owner: Actor, guest: Actor
) -> None:
    await _seed(store, me)
    pid = (
        await tools.ok(me, "merge_identity", duplicate=DUP, canonical=OWNER, reason="Same person")
    )["proposal_id"]
    # Over MCP nobody accepts it, not even a connection with owner trust and admin.
    for actor in (owner, guest):
        error = await tools.error(actor, "accept_proposal", proposal_id=pid, reason="Confirmed")
        assert "owner only, in the portal" in error
    with pytest.raises(WikiError, match="owner only"):
        await store.accept_proposal(pid, "Confirmed", me)
    assert "canonical" not in _fm(store, DUP)
    # The owner in the portal: full consolidation, leaving a minimal redirect.
    result = await store.accept_proposal(pid, "Yes, that is me", OWNER_ACTOR)
    assert result.status == "committed" and result.id == DUP
    dup = _fm(store, DUP)
    assert dup["canonical"] == OWNER and dup["status"] == "deprecated"
    assert "canonical page" in parse_document(store.read_text_file(DUP + ".md")).body
    assert "- 2026-09: Lives in Ghent." in parse_document(store.read_text_file(OWNER + ".md")).body
    assert _fm(store, OWNER)["aliases"] == ["Maarten van den Hof"]
    assert store.list_proposals() == []


async def test_rejecting_over_mcp_still_works(store: WikiStore, tools: Tools, me: Actor) -> None:
    await _seed(store, me)
    pid = (
        await tools.ok(me, "merge_identity", duplicate=DUP, canonical=OWNER, reason="Same person")
    )["proposal_id"]
    admin = Actor("claude.ai", Trust.AGENT, frozenset({"wiki:read", "wiki:admin"}))
    assert (await tools.ok(admin, "reject_proposal", proposal_id=pid, reason="Not the same"))[
        "status"
    ] == ("rejected")
    assert "canonical" not in _fm(store, DUP)


async def test_merge_refusals(store: WikiStore, tools: Tools, me: Actor, guest: Actor) -> None:
    await _seed(store, me)
    secret = {"type": "person", "title": "Best friend", "description": "A friend.", "private": True}
    await store.write_concept("wiki/people/best-friend", secret, "Private.", "test", me)
    await store.write_concept("wiki/people/other", person("Other"), "Other.", "test", me)
    raw = (await store.ingest_source("Chat", "We talked.", None, "agent", me)).id
    cases = [
        (me, DUP, DUP, "into itself"),
        (me, OWNER, DUP, "always the canonical page"),
        (me, "wiki/people/best-friend", FRIEND, "private page with a non-private"),
        (me, FRIEND, "wiki/people/best-friend", "private page with a non-private"),
        (guest, FRIEND, "wiki/people/best-friend", "no page at"),  # private: as if missing
        (me, DUP, "wiki/people/nobody", "no page at"),
        (me, raw, OWNER, "not a concept page"),
    ]
    for actor, dup, canon, why in cases:
        error = await tools.error(
            actor, "merge_identity", duplicate=dup, canonical=canon, reason="x y"
        )
        assert why in error, (dup, canon, error)
    # No chains: into a merged page, or merging a page others are merged into.
    await _accept(store, tools, me, DUP, OWNER)
    error = await tools.error(
        me, "merge_identity", duplicate="wiki/people/other", canonical=DUP, reason="x y"
    )
    assert f"merged into {OWNER}" in error and "no chains" in error
    error = await tools.error(me, "merge_identity", duplicate=DUP, canonical=FRIEND, reason="x y")
    assert "merged into" in error
    await _accept(store, tools, me, "wiki/people/other", FRIEND)
    error = await tools.error(me, "merge_identity", duplicate=FRIEND, canonical=OWNER, reason="x y")
    assert "no chains" in error


async def test_a_duplicate_with_canonical_is_refused(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    """A hand-edited `canonical` counts like an accepted merge: the page is merged already."""
    await _seed(store, me)
    path = store.root / (DUP + ".md")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "status: stable\n", f"status: deprecated\ncanonical: {FRIEND}\n"
        ),
        encoding="utf-8",
    )
    store.reindex(PurePosixPath(DUP + ".md"))
    error = await tools.error(me, "merge_identity", duplicate=DUP, canonical=OWNER, reason="x y")
    assert f"merged into {FRIEND} already" in error


async def _accept(store: WikiStore, tools: Tools, me: Actor, dup: str, canon: str) -> None:
    pid = (await tools.ok(me, "merge_identity", duplicate=dup, canonical=canon, reason="Same"))[
        "proposal_id"
    ]
    await store.accept_proposal(pid, "Confirmed", OWNER_ACTOR)


async def test_acceptance_checks_again(store: WikiStore, tools: Tools, me: Actor) -> None:
    await _seed(store, me)
    pid = (
        await tools.ok(me, "merge_identity", duplicate=DUP, canonical=OWNER, reason="Same person")
    )["proposal_id"]
    await store.set_private(DUP, True, "personal", me)  # now private, the profile is not
    with pytest.raises(WikiError, match="private page with a non-private"):
        await store.accept_proposal(pid, "Confirmed", OWNER_ACTOR)
    assert "canonical" not in _fm(store, DUP)


# ------------------------------------------------------------------ legacy redirect pages

LEGACY = "wiki/people/owner"
LEGACY_TITLE = "Eigenaar (redirect)"


async def _seed_legacy(store: WikiStore, me: Actor) -> None:
    """A hand-made redirect page of the owner: deprecated, no `canonical`, linking the duplicate."""
    legacy = {**person(LEGACY_TITLE), "status": "deprecated", "tags": ["redirect", "persoonlijk"]}
    body = "See [Maarten](/wiki/people/maarten-van-den-hof.md)."
    await store.write_concept(LEGACY, legacy, body, "test", me)
    fan = {"type": "concept", "title": "Fan club", "description": "Fans.", "related": [LEGACY]}
    await store.write_concept("wiki/concepts/fan-club", fan, "Fans of the owner.", "test", me)


async def test_a_legacy_redirect_page_is_merged_with_a_clean_alias(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    await _seed(store, me)
    await _seed_legacy(store, me)
    proposed = await tools.ok(
        me, "merge_identity", duplicate=LEGACY, canonical=OWNER, reason="Same person"
    )
    body = store.read_text_file(proposed["proposal_id"] + ".md")
    assert f'`{OWNER}` gets "Eigenaar" in `aliases`' in body
    await store.accept_proposal(proposed["proposal_id"], "Yes, that is me", OWNER_ACTOR)
    legacy = _fm(store, LEGACY)
    assert legacy["canonical"] == OWNER and legacy["status"] == "deprecated"
    assert _fm(store, OWNER)["aliases"] == ["Eigenaar"]


async def test_two_duplicates_merged_into_the_profile(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    """The live case: the person page of the owner first, then the old redirect page linking to
    it; both straight into the profile (no chain), and both pages' backlinks count for it."""
    await _seed(store, me)
    await _seed_legacy(store, me)
    await _accept(store, tools, me, DUP, OWNER)
    await _accept(store, tools, me, LEGACY, OWNER)
    assert _fm(store, OWNER)["aliases"] == ["Maarten van den Hof", "Eigenaar"]
    for dup in (DUP, LEGACY):
        assert _fm(store, dup)["canonical"] == OWNER
        read = await tools.ok(me, "read_concept", path=dup)
        assert read["id"] == OWNER and read["redirected_from"] == dup
    back = {b["id"] for b in (await tools.ok(me, "get_backlinks", path=OWNER))["backlinks"]}
    assert {FRIEND, NOTE, "wiki/concepts/fan-club"} <= back
    ids = [r["id"] for r in (await tools.ok(me, "search_wiki", query="Eigenaar"))["results"]]
    assert LEGACY not in ids and DUP not in ids


def test_alias_from_title() -> None:
    assert alias_from_title("Eigenaar (redirect)") == "Eigenaar"
    assert alias_from_title("  Jane   Doe (deprecated) ") == "Jane Doe"
    assert alias_from_title("(redirect)") == ""
    assert alias_from_title("Acme (NL) BV") == "Acme (NL) BV"  # only a trailing one


async def test_no_alias_that_the_canonical_page_has_already(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    await _seed(store, me)
    twin = {**person("Owner Profile (old)"), "status": "deprecated"}
    await store.write_concept("wiki/people/old-profile", twin, "Old.", "test", me)
    await _accept(store, tools, me, "wiki/people/old-profile", OWNER)
    assert "aliases" not in _fm(store, OWNER)


# ------------------------------------------------------------------ the redirect


async def test_read_and_fetch_redirect(store: WikiStore, tools: Tools, me: Actor) -> None:
    await _merged(store, tools, me)
    read = await tools.ok(me, "read_concept", path=DUP)
    assert read["id"] == OWNER and read["redirected_from"] == DUP
    assert f"merged into {OWNER}" in read["notice"]
    assert read["frontmatter"]["aliases"] == ["Maarten van den Hof"]
    itself = await tools.ok(me, "read_concept", path=DUP, follow_canonical=False)
    assert itself["id"] == DUP and "redirected_from" not in itself
    assert itself["frontmatter"]["canonical"] == OWNER
    fetched = await tools.ok(me, "fetch", id=DUP)
    assert fetched["id"] == OWNER and fetched["metadata"]["redirected_from"] == DUP
    assert fetched["text"].startswith(f"[{DUP} is a duplicate merged into {OWNER}")
    assert fetched["url"].endswith("/page/" + OWNER)
    plain = await tools.ok(me, "read_concept", path=OWNER)
    assert "redirected_from" not in plain


async def test_search_lists_only_the_canonical_page(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    await _merged(store, tools, me)
    for tool in ("search", "search_wiki"):
        ids = [r["id"] for r in (await tools.ok(me, tool, query="Maarten van den Hof"))["results"]]
        assert OWNER in ids and DUP not in ids, (tool, ids)


async def test_backlinks_of_duplicates_count_for_the_canonical_page(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    await _merged(store, tools, me)
    back = await tools.ok(me, "get_backlinks", path=OWNER)
    assert {b["id"] for b in back["backlinks"]} >= {FRIEND, NOTE}
    via = await tools.ok(me, "get_backlinks", path=DUP)
    assert via["id"] == OWNER and via["redirected_from"] == DUP
    assert via["backlinks"] == back["backlinks"]
    assert set((await tools.ok(me, "read_concept", path=OWNER))["backlinks"]) >= {FRIEND, NOTE}
    # The links themselves are not rewritten.
    assert "/wiki/people/maarten-van-den-hof.md" in store.read_text_file(FRIEND + ".md")


async def test_writes_to_a_duplicate_are_refused(store: WikiStore, tools: Tools, me: Actor) -> None:
    await _merged(store, tools, me)
    error = await tools.error(
        me, "append_to_concept", path=DUP, section="Notes", content="- x", reason="test"
    )
    assert f"write to {OWNER} instead" in error
    with pytest.raises(WikiError, match="merged into"):
        await store.write_concept(DUP, person("Maarten van den Hof"), "Again.", "test", me)
    with pytest.raises(WikiError, match="merged into"):
        await store.link_concepts(DUP, FRIEND, "knows", "test", me)
    # Linking to it is fine: the link reaches the canonical page.
    await store.link_concepts(FRIEND, DUP, "knows", "test", me)


async def test_redirect_never_reveals_a_private_page(
    store: WikiStore, tools: Tools, me: Actor, guest: Actor
) -> None:
    """A hand-edited redirect from a non-private page to a private one (merge_identity refuses
    that pairing): for a caller without wiki:private the duplicate is missing too."""
    secret = {"type": "person", "title": "Best friend", "description": "A friend.", "private": True}
    await store.write_concept("wiki/people/best-friend", secret, "Private.", "test", me)
    await store.write_concept("wiki/people/bf", person("BF"), "Nickname page.", "test", me)
    path = store.root / "wiki/people/bf.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "status: stable\n", "status: deprecated\ncanonical: wiki/people/best-friend\n"
        ),
        encoding="utf-8",
    )
    store.reindex(PurePosixPath("wiki/people/bf.md"))
    for tool, args in (
        ("read_concept", {"path": "wiki/people/bf"}),
        ("fetch", {"id": "wiki/people/bf"}),
    ):
        assert "no page at" in await tools.error(guest, tool, **args)
    assert "no page at" in await tools.error(guest, "get_backlinks", path="wiki/people/bf")
    assert (await tools.ok(me, "read_concept", path="wiki/people/bf"))[
        "id"
    ] == "wiki/people/best-friend"
    # A redirect to a missing page answers the page itself.
    path.write_text(
        path.read_text(encoding="utf-8").replace("wiki/people/best-friend", "wiki/people/nobody"),
        encoding="utf-8",
    )
    store.reindex(PurePosixPath("wiki/people/bf.md"))
    assert (await tools.ok(guest, "read_concept", path="wiki/people/bf"))["id"] == "wiki/people/bf"


# ------------------------------------------------------------------ the owner is no person page


async def test_no_person_page_for_the_owner(store: WikiStore, tools: Tools, me: Actor) -> None:
    owner = {
        "type": "owner",
        "title": "Owner profile",
        "description": "The owner.",
        "aliases": ["Maarten van den Hof"],
    }
    await store.write_concept(OWNER, owner, "# Owner profile", "test", me)
    error = await tools.error(
        me,
        "write_concept",
        path=DUP,
        frontmatter=person("  maarten VAN den   hof "),
        body="The owner.",
        reason="test",
    )
    assert "is the owner" in error and OWNER in error
    assert not store.exists(PurePosixPath(DUP + ".md"))
    # Only an exact (normalised) match: someone else with a similar name is fine.
    await store.write_concept(
        "wiki/people/maarten-de-vries", person("Maarten de Vries"), "Hi.", "test", me
    )
    # Batch filing refuses the page too.
    src = (await store.ingest_source("Chat", "We talked.", None, "agent", me)).id
    page = FilingPage(
        DUP,
        (FilingSection("Facts", ("- 2026-09: Lives in Ghent.",)),),
        type="person",
        title="Maarten van den Hof",
        description="The owner.",
    )
    with pytest.raises(WikiError, match="is the owner"):
        await store.file_conversation([src], [page], "Filed", me)
    assert store.owner_names() == {
        "owner profile",
        "maarten van den hof",
        *("owner", "the owner", "eigenaar", "de eigenaar"),
    }


async def test_no_person_page_for_a_generic_owner_word(
    store: WikiStore, tools: Tools, me: Actor
) -> None:
    """Generic words like "Owner" or "Eigenaar" are the owner too, even without a profile."""
    for title in ("Eigenaar", "  the OWNER ", "De eigenaar"):
        error = await tools.error(
            me,
            "write_concept",
            path="wiki/people/owner",
            frontmatter=person(title),
            body="The owner.",
            reason="test",
        )
        assert "is the owner" in error and OWNER in error
    assert not store.exists(PurePosixPath("wiki/people/owner.md"))
    # A name merely containing the word is someone else.
    await store.write_concept("wiki/people/eigenaar-jan", person("Eigenaar Jan"), "Hi.", "test", me)

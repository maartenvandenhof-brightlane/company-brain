import pytest

from wiki_mcp.wiki.okf import (
    DocumentError,
    conformance_errors,
    parse_document,
    serialize_document,
    validate_concept_input,
)

TYPES = ["person", "concept", "source"]


def test_roundtrip_preserves_unknown_keys_and_order() -> None:
    text = "---\ntype: person\ntitle: A\nx_custom: 1\ngenerated:\n  by: a/b\n  at: 2026-06-20T22:53:05Z\n---\n\nBody\n"
    doc = parse_document(text)
    assert doc.frontmatter == {
        "type": "person",
        "title": "A",
        "x_custom": 1,
        "generated": {"by": "a/b", "at": "2026-06-20T22:53:05Z"},
    }
    out = serialize_document(doc.frontmatter, doc.body)
    assert out.startswith("---\ntype: person\ntitle: A\ngenerated:")
    assert "x_custom: 1" in out
    assert parse_document(out).frontmatter == doc.frontmatter


def test_no_frontmatter() -> None:
    doc = parse_document("# Title\n")
    assert doc.frontmatter is None and doc.type is None


@pytest.mark.parametrize(
    "text",
    [
        "---\ntype: [unclosed\n---\n",
        "---\n- a list\n---\n",
        "---\ntype: x\n",
        "---\na: &x [1]\nb: *x\n---\n",
        "---\n!!python/object:os.system {}\n---\n",
    ],
)
def test_malformed_frontmatter(text: str) -> None:
    with pytest.raises(DocumentError):
        parse_document(text)


def test_validate_ok_and_normalizes() -> None:
    c = validate_concept_input(
        {
            "type": "person",
            "title": "  Jane   Doe ",
            "description": "A person.",
            "tags": "Friends, work",
            "sources": ["raw/2026-01-01-x", {"resource": "https://example.com/a", "title": "A"}],
            "related": ["/wiki/people/john.md"],
            "id": "ignored",
            "trust": "owner",
            "my_field": ["a", 1],
        },
        known_types=TYPES,
    )
    assert c.title == "Jane Doe"
    assert c.tags == ["friends", "work"]
    assert c.sources[0] == {"id": "2026-01-01-x", "resource": "/raw/2026-01-01-x.md"}
    assert c.sources[1]["resource"] == "https://example.com/a"
    assert c.related == ["wiki/people/john"]
    assert c.extra == {"my_field": ["a", 1]}
    assert c.raw_source_ids() == ["raw/2026-01-01-x"]


def test_validate_collects_all_errors() -> None:
    with pytest.raises(DocumentError) as exc:
        validate_concept_input(
            {
                "type": "alien",
                "status": "active",
                "tags": ["ok", "Not Ok!"],
                "sources": ["wiki/people/x"],
                "Bad-Key": 1,
                "stale_after": "tomorrow",
            },
            known_types=TYPES,
        )
    msgs = " | ".join(exc.value.errors)
    for needle in (
        "'type'",
        "'title'",
        "'description'",
        "'status'",
        "invalid tag",
        "raw/",
        "Bad-Key",
        "stale_after",
    ):
        assert needle in msgs


def test_conformance() -> None:
    assert conformance_errors("x.md", parse_document("# no fm\n"), is_root=False)
    assert conformance_errors("x.md", parse_document("---\ntitle: a\n---\n"), is_root=False)
    assert not conformance_errors("x.md", parse_document("---\ntype: a\n---\n"), is_root=False)
    root_index = parse_document('---\nokf_version: "0.2"\n---\n# I\n')
    assert not conformance_errors("index.md", root_index, is_root=True)
    assert conformance_errors("index.md", root_index, is_root=False)
    assert not conformance_errors("log.md", parse_document("# Log\n"), is_root=True)

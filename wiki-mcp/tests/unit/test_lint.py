import datetime as dt
from pathlib import PurePosixPath as P

from wiki_mcp.wiki.index import fts_query
from wiki_mcp.wiki.links import extract_links
from wiki_mcp.wiki.lint import lint_bundle


def page(t: str, title: str, body: str = "", **fm: str) -> str:
    extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
    return f"---\ntype: {t}\ntitle: {title}\ndescription: d\n{extra}---\n{body}\n"


def codes(report) -> set[str]:  # type: ignore[no-untyped-def]
    return {i.code for i in report.issues}


def test_detects_problems(registry) -> None:  # type: ignore[no-untyped-def]
    now = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    files = [
        (
            P("wiki/people/a.md"),
            page("person", "A", "[B](/wiki/people/b.md) [gone](/wiki/people/zzz.md)"),
        ),
        (
            P("wiki/people/b.md"),
            page("person", "A", "[a](./a.md)", stale_after="2026-01-01T00:00:00Z"),
        ),
        (P("wiki/people/c.md"), page("concept", "C", updated="2020-01-01T00:00:00Z")),
        (P("wiki/people/d.md"), "no frontmatter"),
        (P("wiki/people/e.md"), page("person", "E", "password: hunter2hunter2")),
        (
            P("wiki/people/f.md"),
            page("person", "F", "", sources="[{resource: /raw/nope.md}]", status="active"),
        ),
        (P("wiki/people/g.md"), page("person", "G", "", id="wrong")),
        (
            P("raw/s.md"),
            "---\ntype: source\ntitle: S\ndescription: d\ncontent_sha256: abc\n---\nx\n",
        ),
        (P("index.md"), "---\ntitle: x\n---\n"),
    ]
    report = lint_bundle(files, registry, now=now)
    assert not report.ok
    for code in (
        "broken-link",
        "stale",
        "wrong-folder",
        "okf-conformance",
        "secret",
        "missing-source",
        "bad-status",
        "id-mismatch",
        "duplicate-title",
        "orphan",
        "source-modified",
        "no-sources",
    ):
        assert code in codes(report), code
    md = report.to_markdown()
    assert "## Errors" in md and "`broken-link`" in md


def test_extract_links() -> None:
    body = "[a](/wiki/x.md) [b](../y.md#h) [c](https://e.com/z.md) ![img](/i.md) `[d](/code.md)`\n```\n[e](/f.md)\n```"
    assert extract_links(body, P("wiki/people/p.md")) == ["wiki/x", "wiki/y"]


def test_fts_query_is_safe() -> None:
    assert fts_query('foo "bar" NOT baz*') == '"foo" AND "bar" AND "not" AND "baz"*'
    assert fts_query('foo "bar" OR baz*') == '"foo" AND "bar" AND "baz"*'  # "or" is filler
    assert fts_query("!!!") is None


def test_citing_a_source_is_provenance_not_a_page_link(registry) -> None:  # type: ignore[no-untyped-def]
    source = "---\ntype: source\nid: raw/chats/claude/s\ntitle: S\ndescription: d\n---\nx\n"
    files = [
        (P("raw/chats/claude/s.md"), source),
        (
            P("wiki/people/a.md"),
            page("person", "A", "", sources="[{id: s, resource: /raw/chats/claude/s.md}]"),
        ),
    ]
    report = lint_bundle(files, registry)
    by_path = {(i.code, i.path) for i in report.issues}
    assert ("orphan", "wiki/people/a.md") in by_path  # still no page links to it
    assert not {c for c, p in by_path if p == "raw/chats/claude/s.md"}
    assert ("no-sources", "wiki/people/a.md") not in by_path


def inference(body: str = "**Inferred, not stated.** x", **fm: str) -> str:
    extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
    return (
        f"---\ntype: inference\ntitle: I\ndescription: d\nkind: induction\n{extra}---\n"
        f"# I\n\n{body}\n"
    )


SUPPORTS = "[{page: wiki/people/a, quote: q, source: raw/s1}, {page: wiki/people/a, quote: r, source: raw/s2}]"


def test_facts_and_inferences(registry) -> None:  # type: ignore[no-untyped-def]
    files = [
        (P("wiki/people/a.md"), page("person", "A", "- 2026-01: x [^s1]")),
        (P("raw/s1.md"), "---\ntype: source\ntitle: S\ndescription: d\n---\nx\n"),
        (P("wiki/inferred/good.md"), inference(supports=SUPPORTS, confidence="medium")),
        (
            P("wiki/people/cites.md"),
            page("person", "Cites", "", sources="[{resource: /wiki/inferred/good.md}]"),
        ),
        (P("wiki/inferred/thin.md"), inference(supports="[{page: wiki/people/a}]")),
        (
            P("wiki/inferred/chain.md"),
            inference(supports="[{page: wiki/people/a}, {page: wiki/inferred/good}]"),
        ),
        (
            P("wiki/inferred/ghost.md"),
            inference(supports="[{page: wiki/people/a}, {page: wiki/people/zzz}]"),
        ),
        (P("wiki/inferred/sure.md"), inference(supports=SUPPORTS, confidence="high")),
        (
            P("wiki/inferred/spec.md"),
            inference(supports=SUPPORTS, confidence="low", speculative="true"),
        ),
        (
            P("wiki/inferred/spec-ok.md"),
            inference(
                "**Speculative, inferred, not stated.** Speculative: x",
                supports=SUPPORTS,
                confidence="low",
                speculative="true",
            ),
        ),
        (
            P("wiki/people/b.md"),
            page("person", "B", "- 2026-01: x [^s1] (superseded 2026-02-01: y)"),
        ),
    ]
    issues = lint_bundle(files, registry).issues
    found = {(i.code, i.path) for i in issues}
    assert ("cites-inference", "wiki/people/cites.md") in found
    assert ("inference-support", "wiki/inferred/thin.md") in found
    assert ("inference-support", "wiki/inferred/chain.md") in found
    assert ("inference-support", "wiki/inferred/ghost.md") in found
    assert ("inference-confidence", "wiki/inferred/sure.md") in found
    assert ("speculative-marking", "wiki/inferred/spec.md") in found
    assert ("bad-superseded", "wiki/people/b.md") in found
    clean = {"wiki/inferred/good.md", "wiki/inferred/spec-ok.md", "wiki/people/a.md"}
    new = {"cites-inference", "inference-support", "inference-confidence", "speculative-marking"}
    assert not [i for i in issues if i.path in clean and i.code in new | {"bad-superseded"}]
    severity = {i.code: i.severity for i in issues}
    assert severity["inference-confidence"] == severity["bad-superseded"] == "warning"
    assert severity["cites-inference"] == severity["speculative-marking"] == "error"

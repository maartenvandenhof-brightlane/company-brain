"""The sanitizer and link rewriting: wiki content is untrusted."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import pytest

from portal.docs import DocsLibrary
from portal.render import render_markdown, render_snippet, sanitize
from portal.wiki import wiki_resolver
from tests.conftest import _find
from tests.htmlcheck import active_content

KNOWN = {"wiki/people/jane-doe", "wiki/concepts/company-brain", "AGENTS"}
SOURCE = PurePosixPath("wiki/concepts/company-brain.md")


def render(text: str) -> str:
    return render_markdown(text, wiki_resolver(SOURCE, KNOWN))


XSS = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "<iframe src=https://evil.example></iframe>",
    '<a href="javascript:alert(1)">x</a>',
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](vbscript:msgbox(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "<javascript:alert(1)>",
    "![x](javascript:alert(1))",
    '![x](https://evil.example/pixel.png "t")',
    "[x](//evil.example/steal)",
    '<div style="background:url(javascript:alert(1))">x</div>',
    '<svg onload=alert(1)><circle r="1"/></svg>',
    '[x](https://ok.example/"onmouseover="alert(1))',
    "<!-- <script>alert(1)</script> -->",
    "| a |\n|---|\n| <script>x</script> |",
    "```html\n<script>alert(1)</script>\n```",
    "a [^x]\n\n[^x]: [x](javascript:alert(1))",
    '[^<script>]\n\n[^<script>]: <img src=x onerror="alert(1)">',
]


@pytest.mark.parametrize("payload", XSS)
def test_no_active_content_survives(payload: str) -> None:
    out = render(payload)
    assert active_content(out) == [], (payload, out)
    assert "evil.example/steal" not in out or 'href="//' not in out


def test_raw_html_is_shown_as_text() -> None:
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in render("<script>alert(1)</script>")


def test_links_rewritten_and_marked() -> None:
    out = render(
        "[abs](/wiki/people/jane-doe.md) [rel](../people/jane-doe.md#role) "
        "[missing](/wiki/people/nobody.md) [ext](https://example.com/a) "
        "[guide](/AGENTS.md) [log](/log.md) [pdf](../../raw/x.pdf) [mail](mailto:a@b.c)"
    )
    assert 'href="/wiki/page/wiki/people/jane-doe"' in out
    assert 'href="/wiki/page/wiki/people/jane-doe#role"' in out
    assert 'href="/wiki/page/wiki/people/nobody" class="broken"' in out
    assert 'href="https://example.com/a" class="external"' in out
    assert 'rel="noopener noreferrer nofollow"' in out
    assert 'href="/wiki/page/AGENTS"' in out
    assert 'href="/wiki/log"' in out
    assert '<a class="broken" title="Not a wiki page: ../../raw/x.pdf"' in out
    assert 'href="mailto:a@b.c"' in out


def test_images_become_links() -> None:
    out = render("![a cat](https://example.com/cat.png)")
    assert "<img" not in out
    assert (
        '<a class="external" href="https://example.com/cat.png"' in out and "[image: a cat]" in out
    )


def test_tables_and_code_render() -> None:
    out = render("| a | b |\n|:--|--:|\n| 1 | 2 |\n\n```\nx < y\n```")
    assert "<table>" in out and "<td>1</td>" in out
    assert "<pre><code>x &lt; y" in out


CHAT = "d24e8f1a-0b2c-4d5e-8f9a-123456789abc"
FILED = (
    f"- 2026-01: Prefers kits over own designs. [^{CHAT}]\n"
    "- 2026-02: Sleeps better. [^abc]\n\n"
    "## Sources\n\n"
    f"[^{CHAT}]: [Planning the shed](/raw/chats/claude/{CHAT}.md), conversation of 2026-01-10\n"
    "[^abc]: [Private conversation](/raw/chats/claude/abc.md)\n"
)


def test_footnotes_cite_their_sources() -> None:
    """Filed pages cite chats as `[^id]` footnotes: a compact citation linking to the raw
    source, and the definitions as a list (the format processing.filing writes)."""
    known = {f"raw/chats/claude/{CHAT}", "raw/chats/claude/abc"}
    out = render_markdown(FILED, wiki_resolver(SOURCE, known))
    assert "[^" not in out
    assert (
        f'<sup class="footnote-ref"><a href="/wiki/page/raw/chats/claude/{CHAT}" id="fnref1" '
        'class="internal" title="Planning the shed"'
    ) in out
    assert (
        '<sup class="footnote-ref"><a href="/wiki/page/raw/chats/claude/abc" id="fnref2" '
        'class="internal" title="Private conversation"'
    ) in out
    notes = out.split('<section class="footnotes">')[1]
    assert '<li id="fn1" class="footnote-item">' in notes
    assert f'href="/wiki/page/raw/chats/claude/{CHAT}" class="internal"' in notes
    assert ">Planning the shed</a>, conversation of 2026-01-10" in notes
    assert ">Private conversation</a>" in notes
    assert '<a href="#fnref1" class="footnote-backref"' in notes
    # a source that is not (yet) in the wiki is still cited, marked like any missing page
    missing = render(FILED)
    assert f'href="/wiki/page/raw/chats/claude/{CHAT}" id="fnref1" class="broken"' in missing


def test_footnotes_are_neutralised() -> None:
    out = render(
        "a [^x] b [^y] c [^undefined] d [^z]\n\n"
        '[^x]: [evil"><script>](javascript:alert(1))\n'
        "[^y]: No link at all.\n"
        "[^z]: [far](//evil.example/steal)\n"
    )
    assert active_content(out) == [], out
    assert "javascript" not in out and 'href="//' not in out
    # no usable link: the citation points to the footnote itself
    assert '<a href="#fn1" id="fnref1" class="internal" title="evil&quot;&gt;&lt;script&gt;"' in out
    assert '<a href="#fn2" id="fnref2" class="internal" title=""' in out
    assert '<a href="#fn3" id="fnref3" class="internal" title="far"' in out
    assert "c [^undefined] d" in out  # undefined references stay text
    # ids only as the footnote rules write them
    assert set(re.findall(r'id="([^"]*)"', out)) == {
        "fnref1",
        "fnref2",
        "fnref3",
        "fn1",
        "fn2",
        "fn3",
    }
    clobber = sanitize('<a id="location" href="#x">x</a><li id="fn1 x">y</li><p id="fn1">z</p>')
    assert "id=" not in clobber


def test_snippet_marks_are_escaped() -> None:
    assert render_snippet("a **<b>hit</b>** c") == "a <mark>&lt;b&gt;hit&lt;/b&gt;</mark> c"


def test_docs_links() -> None:
    lib = DocsLibrary(_find("docs"))
    readme = lib.pages["readme"]
    html = lib.render(readme)
    assert 'href="/docs/wiki-format"' in html
    assert 'href="/docs/security"' in html
    sec = lib.render(lib.pages["security"])
    assert "<script" not in sec
    resolve = lib.resolver(lib.pages["tailscale"])
    assert resolve("../README.md").href == "/docs/readme"
    assert resolve("security.md#private-pages").href == "/docs/security#private-pages"
    assert resolve("../compose.yaml").href is None

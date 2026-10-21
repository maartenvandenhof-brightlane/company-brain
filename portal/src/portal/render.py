"""Markdown to safe HTML for untrusted wiki content (and the repository docs).

Three layers, each sufficient on its own for scripts:
  1. markdown-it-py with raw HTML disabled: `<script>` in a page is rendered as text.
  2. Every link and image target goes through a resolver: only http(s), mailto and
     bundle links survive; bundle links are rewritten to portal routes.
  3. nh3 (the Rust `ammonia` sanitizer) with a small tag/attribute/class allowlist.
Footnote citations (`[^id]`, as filed pages cite their chat sources) link straight to
the footnote's source through the same resolver; the definitions become a list at the end.
And the CSP forbids inline scripts, styles and remote images on top of that.
Images are never embedded: they become links, so a page cannot make the browser
fetch anything by being viewed.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import nh3
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from mdit_py_plugins.footnote import footnote_plugin

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
SAFE_SCHEMES = frozenset({"http", "https", "mailto"})

ALLOWED_TAGS = {
    "a", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "s",
    "code", "pre", "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "sup", "section",
}  # fmt: skip
# `class` is governed by ALLOWED_CLASSES only (ammonia rejects it in both places).
# `id` only for the footnote anchors, and only with the values the footnote rules emit.
ALLOWED_ATTRIBUTES = {"a": {"href", "title", "id"}, "ol": {"start"}, "li": {"id"}}
ALLOWED_CLASSES = {
    "a": {"internal", "external", "broken", "footnote-backref"},
    "sup": {"footnote-ref"},
    "hr": {"footnotes-sep"},
    "section": {"footnotes"},
    "ol": {"footnotes-list"},
    "li": {"footnote-item"},
}
_FOOTNOTE_ID = re.compile(r"^fn(ref)?[0-9]+(:[0-9]+)?$")


@dataclass(frozen=True)
class Link:
    """What a link target becomes. href=None renders the label without a link."""

    href: str | None
    css: str = "internal"
    title: str = ""


LinkResolver = Callable[[str], Link]


def external_or_none(target: str) -> Link | None:
    """Handle URL-with-scheme and fragment targets; None means 'a local path, resolve it'."""
    target = target.strip()
    match = _SCHEME.match(target)
    if match:
        if match.group(1).lower() in SAFE_SCHEMES:
            return Link(target, "external")
        return Link(None, "broken", "Link removed: unsupported URL scheme")
    if target.startswith("//"):
        return Link(None, "broken", "Link removed: protocol-relative URL")
    if target.startswith("#"):
        return Link(target, "internal")
    return None


def _render_link_open(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    token = tokens[idx]
    resolver: LinkResolver = env["resolve"]
    link = external_or_none(str(token.attrGet("href") or "")) or resolver(
        str(token.attrGet("href") or "")
    )
    token.attrs = {}
    if link.href is not None:
        token.attrSet("href", link.href)
    token.attrSet("class", link.css)
    if link.title:
        token.attrSet("title", link.title)
    return self.renderToken(tokens, idx, options, env)


def _render_image(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Images become links (never fetched automatically)."""
    token = tokens[idx]
    src = str(token.attrGet("src") or "")
    alt = self.renderInlineAsText(token.children or [], options, env) or "image"
    resolver: LinkResolver = env["resolve"]
    link = external_or_none(src) or resolver(src)
    label = html.escape(f"[image: {alt}]")
    if link.href is None:
        return f'<a class="broken" title="{html.escape(link.title)}">{label}</a>'
    return f'<a class="{link.css}" href="{html.escape(link.href)}">{label}</a>'


def _footnote_sources(state: StateCore) -> None:
    """Note each footnote's first link (target and text): its citations link straight there."""
    sources: dict[int, tuple[str, str]] = {}
    current: int | None = None
    for token in state.tokens:
        if token.type == "footnote_open":
            current = token.meta["id"]
        elif token.type == "footnote_close":
            current = None
        elif current is not None and current not in sources and token.type == "inline":
            children = token.children or []
            opens = [i for i, child in enumerate(children) if child.type == "link_open"]
            if opens:
                text = []
                for child in children[opens[0] + 1 :]:
                    if child.type == "link_close":
                        break
                    text.append(child.content)
                sources[current] = (str(children[opens[0]].attrGet("href") or ""), "".join(text))
    state.env["footnote_sources"] = sources


def _render_footnote_ref(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """A compact `[n]` citation linking to the footnote's source, or to the footnote itself
    when it has no usable link."""
    meta = tokens[idx].meta
    n, sub = meta["id"] + 1, meta.get("subId", 0)
    target, text = env.get("footnote_sources", {}).get(meta["id"], ("", ""))
    resolver: LinkResolver = env["resolve"]
    link = (external_or_none(target) or resolver(target)) if target else Link(None)
    if link.href is None:
        link = Link(f"#fn{n}")
    suffix = f":{sub}" if sub else ""
    return (
        f'<sup class="footnote-ref"><a href="{html.escape(str(link.href))}" '
        f'id="fnref{n}{suffix}" class="{link.css}" title="{html.escape(text or link.title)}">'
        f"[{n}{suffix}]</a></sup>"
    )


class _Markdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        # Our link rule decides what survives; markdown-it's own check would drop targets
        # silently (turning them into plain text), which hides links instead of marking them.
        return True


def _build_md() -> MarkdownIt:
    md = _Markdown("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.use(footnote_plugin, inline=False)
    md.core.ruler.after("footnote_tail", "footnote_sources", _footnote_sources)
    md.add_render_rule("link_open", _render_link_open)
    md.add_render_rule("footnote_ref", _render_footnote_ref)
    md.add_render_rule("image", _render_image)
    return md


_MD = _build_md()


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "id" and not _FOOTNOTE_ID.match(value):
        return None
    return value


def sanitize(fragment: str) -> str:
    return nh3.clean(
        fragment,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        allowed_classes=ALLOWED_CLASSES,
        url_schemes=set(SAFE_SCHEMES),
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def render_markdown(text: str, resolve: LinkResolver) -> str:
    """Render untrusted markdown to sanitized HTML; `resolve` maps local link targets."""
    env: dict[str, Any] = {"resolve": resolve}
    return sanitize(_MD.render(text, env))


def render_snippet(snippet: str) -> str:
    """FTS snippet with **match** markers -> escaped text with <mark> highlights."""
    parts = html.escape(snippet).split("**")
    return "".join(f"<mark>{p}</mark>" if i % 2 else p for i, p in enumerate(parts))

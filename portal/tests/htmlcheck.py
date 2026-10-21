"""Structural XSS check: parse rendered HTML and list anything active."""

from __future__ import annotations

from html.parser import HTMLParser

DANGEROUS_TAGS = {"script", "img", "iframe", "svg", "object", "embed", "style", "link", "meta",
                  "form", "input", "base", "video", "audio", "source", "math", "div"}  # fmt: skip
SAFE_SCHEMES = ("http://", "https://", "mailto:", "/", "#")


class _Finder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DANGEROUS_TAGS:
            self.problems.append(f"<{tag}>")
        for name, value in attrs:
            if name.startswith("on") or name in ("style", "srcdoc", "formaction"):
                self.problems.append(f"{tag}[{name}]")
            if name in ("href", "src") and value is not None:
                v = value.strip().lower()
                if not v.startswith(SAFE_SCHEMES) or v.startswith("//"):
                    self.problems.append(f"{tag}[{name}={value!r}]")


def active_content(html: str) -> list[str]:
    finder = _Finder()
    finder.feed(html)
    return finder.problems

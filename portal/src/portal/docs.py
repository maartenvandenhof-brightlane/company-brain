"""The repository docs (README.md and docs/*.md, copied into the image at build time).

  GET /docs          README with the list of guides
  GET /docs/{slug}   one guide

Links between docs are rewritten to portal routes; links to other repository files
are shown but not followed. Rendered through the same sanitizer as the wiki.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from portal.render import Link, LinkResolver, render_markdown
from portal.sessions import Session
from portal.web import Portal, owner


@dataclass(frozen=True)
class DocPage:
    slug: str
    title: str
    text: str
    repo_path: PurePosixPath


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


class DocsLibrary:
    def __init__(self, docs_dir: Path) -> None:
        self.pages: dict[str, DocPage] = {}
        if not docs_dir.is_dir():
            return
        for path in sorted(docs_dir.glob("*.md"), key=lambda p: (p.name != "README.md", p.name)):
            if path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8")
            if path.name == "README.md":
                slug, repo = "readme", PurePosixPath("README.md")
            else:
                slug, repo = path.stem, PurePosixPath("docs") / path.name
            self.pages[slug] = DocPage(slug, _title(text, path.stem), text, repo)
        self._by_repo = {str(p.repo_path): p.slug for p in self.pages.values()}

    def resolver(self, page: DocPage) -> LinkResolver:
        def resolve(target: str) -> Link:
            path, _, fragment = target.partition("#")
            path = path.split("?", 1)[0]
            joined = posixpath.normpath(posixpath.join(str(page.repo_path.parent), path))
            slug = self._by_repo.get(joined.lstrip("./"))
            if slug is None:
                return Link(None, "broken", f"{joined}: a file in the repository, not a guide")
            return Link(f"/docs/{quote(slug)}" + (f"#{quote(fragment)}" if fragment else ""))

        return resolve

    def render(self, page: DocPage) -> str:
        return render_markdown(page.text, self.resolver(page))


class DocsRoutes:
    def __init__(self, portal: Portal, library: DocsLibrary) -> None:
        self.p = portal
        self.library = library
        self._html = {slug: library.render(page) for slug, page in library.pages.items()}

    def routes(self) -> list[Route]:
        return [
            Route("/docs", owner(self.p)(self.show)),
            Route("/docs/{slug}", owner(self.p)(self.show)),
        ]

    async def show(self, request: Request, session: Session, _: FormData | None) -> Response:
        slug = request.path_params.get("slug", "readme")
        page = self.library.pages.get(slug)
        if page is None:
            return self.p.render(
                request, "error.html", session, status=404, message="No such guide."
            )
        return self.p.render(
            request,
            "docs.html",
            session,
            page=page,
            body=self._html[slug],
            pages=list(self.library.pages.values()),
        )

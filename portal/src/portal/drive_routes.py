"""Google Drive by hand (the brain's folder mode): its Markdown export, to upload to Drive.

  GET /drive                  how to upload it, and the text of "00 Start here.md" and
                              "00 Index.md" to copy
  GET /drive/Brain.zip        the whole export, unzipping to a folder named Brain
  GET /drive/start-here.md    "00 Start here.md" alone
  GET /drive/index.md         "00 Index.md" alone

The export lives in the brain's data/drive/Brain, which the portal does not mount: all of it
comes through wiki-mcp's portal API.
"""

from __future__ import annotations

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from portal.sessions import Session
from portal.web import Portal, owner
from portal.wikiclient import NotFound, WikiClient, WikiUnavailable

NO_EXPORT = "There is no export yet. Choose 'Google Drive by hand' in the setup wizard first."
# route -> (key in GET /drive/start, file name)
FILES = {
    "start-here.md": ("start_here", "00 Start here.md"),
    "index.md": ("index", "00 Index.md"),
}


def _attachment(content: bytes | str, media_type: str, name: str) -> Response:
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


class DriveRoutes:
    def __init__(self, portal: Portal, wiki: WikiClient) -> None:
        self.p = portal
        self.wiki = wiki

    def routes(self) -> list[Route]:
        return [
            Route("/drive", owner(self.p)(self.page)),
            Route("/drive/Brain.zip", owner(self.p)(self.zip)),
            *(Route(f"/drive/{name}", owner(self.p)(self.file)) for name in FILES),
        ]

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        files, error = {}, None
        try:
            files = await self.wiki.drive_start()
        except NotFound:
            error = NO_EXPORT
        except (WikiUnavailable, ValueError) as exc:
            error = str(exc)
        return self.p.render(request, "drive.html", session, files=files, error=error)

    @staticmethod
    def _back(session: Session, exc: Exception) -> Response:
        session.notify(NO_EXPORT if isinstance(exc, NotFound) else str(exc), ok=False)
        return RedirectResponse("/drive", status_code=303)

    async def zip(self, request: Request, session: Session, _: FormData | None) -> Response:
        try:
            data = await self.wiki.drive_zip()
        except (NotFound, WikiUnavailable) as exc:
            return self._back(session, exc)
        return _attachment(data, "application/zip", "Brain.zip")

    async def file(self, request: Request, session: Session, _: FormData | None) -> Response:
        key, name = FILES[request.url.path.removeprefix("/drive/")]
        try:
            files = await self.wiki.drive_start()
        except (NotFound, WikiUnavailable, ValueError) as exc:
            return self._back(session, exc)
        return _attachment(files[key], "text/markdown", name)

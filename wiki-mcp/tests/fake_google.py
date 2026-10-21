"""An in-memory stand-in for the Google endpoints the brain calls (OAuth, Drive, Tasks, Keep),
served through httpx2.MockTransport: no network in the tests."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import jwt

from wiki_mcp.brain import write_secret
from wiki_mcp.google import (
    DRIVE_SCOPE,
    GOOGLE_CLIENT,
    GOOGLE_TOKEN,
    TASKS_SCOPE,
    Google,
)

CLIENT_ID = "123-abc.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"
REFRESH = "1//refresh-token"


def _id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


class FakeGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (method, path) of every API call
        self.files: dict[str, dict[str, Any]] = {}  # id -> {name, mimeType, parents, html}
        self.tasks: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self.challenges: dict[str, str] = {}  # code -> PKCE challenge
        self.token_forms: list[dict[str, str]] = []
        self.fail: dict[str, int] = {}  # a Doc name -> the HTTP status its uploads get
        self.queue: list[int] = []  # statuses for the next API calls (401, 429, ...)
        self.n = 0

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def google(
        self, secrets_dir: Path, *, scopes: tuple[str, ...] = (DRIVE_SCOPE, TASKS_SCOPE)
    ) -> Google:
        """A Google client that is already connected with `scopes`."""
        google = Google(secrets_dir, transport=self.transport())
        google.sleep = self._sleep
        write_secret(
            secrets_dir,
            GOOGLE_CLIENT,
            json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}),
        )
        if scopes:
            token = {
                "refresh_token": REFRESH,
                "email": "owner@acme.test",
                "scope": " ".join(scopes),
            }
            write_secret(secrets_dir, GOOGLE_TOKEN, json.dumps(token))
        return google

    async def _sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", str(seconds)))

    def _id(self) -> str:
        self.n += 1
        return f"f{self.n}"

    def docs(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.files.items() if v["mimeType"].endswith("document")}

    def by_name(self, name: str) -> dict[str, Any]:
        (found,) = [f for f in self.files.values() if f["name"] == name]
        return found

    # ------------------------------------------------------------ the endpoints

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com":
            return self._token(request)
        self.calls.append((request.method, url.path))
        assert request.headers["authorization"].startswith("Bearer ")
        if self.queue:
            return httpx2.Response(self.queue.pop(0), json={"error": {"message": "queued"}})
        if url.host == "www.googleapis.com":
            return self._drive(request)
        if url.host == "tasks.googleapis.com":
            return self._tasks(request)
        if url.host == "keep.googleapis.com":
            return self._keep(request)
        return httpx2.Response(404)

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/revoke":
            self.calls.append(("POST", "/revoke"))
            return httpx2.Response(200)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_forms.append(form)
        self.n += 1
        grant = form["grant_type"]
        if grant == "authorization_code":
            verifier = form["code_verifier"].encode()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).decode()
            if self.challenges.get(form["code"]) != challenge.rstrip("="):
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(
                200,
                json={
                    "access_token": f"at{self.n}",
                    "expires_in": 3599,
                    "refresh_token": REFRESH,
                    "scope": f"openid {DRIVE_SCOPE} {TASKS_SCOPE}",
                    "id_token": _id_token("owner@acme.test"),
                },
            )
        if grant == "refresh_token":
            if form["refresh_token"] != REFRESH or form["client_secret"] != CLIENT_SECRET:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"access_token": f"at{self.n}", "expires_in": 3599})
        claims = jwt.decode(form["assertion"], options={"verify_signature": False})
        self.token_forms[-1] = {**form, **claims}
        return httpx2.Response(200, json={"access_token": f"sa{self.n}", "expires_in": 3599})

    def _drive(self, request: httpx2.Request) -> httpx2.Response:
        path, params = request.url.path, request.url.params
        if path == "/drive/v3/files" and request.method == "GET":
            name = params["q"].split("'")[1]
            hits = [
                {"id": k}
                for k, f in self.files.items()
                if f["name"] == name and f["mimeType"].endswith("folder")
            ]
            return httpx2.Response(200, json={"files": hits})
        if path == "/drive/v3/files" and request.method == "POST":
            fid = self._id()
            self.files[fid] = {**json.loads(request.content), "html": ""}
            return httpx2.Response(200, json={"id": fid})
        if path.startswith("/drive/v3/files/") and request.method == "GET":
            fid = path.rsplit("/", 1)[1]
            if fid not in self.files:
                return httpx2.Response(404, json={"error": {"message": "File not found"}})
            return httpx2.Response(200, json={"id": fid, "trashed": False})
        if path.startswith("/upload/drive/v3/files"):
            assert params["uploadType"] == "multipart"
            meta, html = self._multipart(request)
            if meta["name"] in self.fail:
                return httpx2.Response(self.fail[meta["name"]], json={"error": {"message": "no"}})
            if request.method == "POST":
                fid = self._id()
                self.files[fid] = {**meta, "html": html}
                return httpx2.Response(200, json={"id": fid})
            fid = path.rsplit("/", 1)[1]
            if fid not in self.files:
                return httpx2.Response(404, json={"error": {"message": "File not found"}})
            self.files[fid].update(meta, html=html)
            return httpx2.Response(200, json={"id": fid})
        return httpx2.Response(405)

    @staticmethod
    def _multipart(request: httpx2.Request) -> tuple[dict[str, Any], str]:
        ctype = request.headers["content-type"]
        assert ctype.startswith("multipart/related; boundary=")
        boundary = ctype.split("boundary=")[1]
        parts = request.content.decode().split(f"--{boundary}")[1:3]
        meta_part, html_part = (p.split("\r\n\r\n", 1) for p in parts)
        assert "application/json" in meta_part[0] and "text/html" in html_part[0]
        return json.loads(meta_part[1]), html_part[1].removesuffix("\r\n")

    def _tasks(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/tasks/v1/lists/@default/tasks"
        params = request.url.params
        assert params.get("showCompleted", "true") == "true"  # completed tasks count too
        start = int(params.get("pageToken", "0"))
        size = int(params["maxResults"])
        page = self.tasks[start : start + min(size, 2)]  # small pages: pagination is exercised
        body: dict[str, Any] = {"items": page}
        if start + 2 < len(self.tasks):
            body["nextPageToken"] = str(start + 2)
        return httpx2.Response(200, json=body)

    def _keep(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            name = request.url.path.removeprefix("/v1/")
            self.notes = [n for n in self.notes if n["name"] != name]
            return httpx2.Response(200, json={})
        start = int(request.url.params.get("pageToken", "0"))
        body: dict[str, Any] = {"notes": self.notes[start : start + 2]}
        if start + 2 < len(self.notes):
            body["nextPageToken"] = str(start + 2)
        return httpx2.Response(200, json=body)

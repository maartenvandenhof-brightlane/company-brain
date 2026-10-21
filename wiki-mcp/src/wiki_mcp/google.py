"""Google sign-in and API calls for the Drive export and the inbox (gdrive and both modes).

Every company uses its own OAuth client: a "Web application" client in its own Google Cloud
project with an "Internal" consent screen, so Google never has to verify the app. The wizard stores
the client's id and secret in secrets/google_client and runs the authorization-code flow with
PKCE (S256) and a single-use state, back to its own localhost callback; the refresh token goes to
secrets/google_token. Access tokens live in memory only. Google Keep cannot be read with a user
token: a service account with domain-wide delegation (secrets/google_keep_service_account)
impersonates one Workspace user. No token or secret is ever logged.

Calls refresh the access token once on 401 and back off on 429, 5xx and Drive's 403 rate limits.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx2
import jwt

from wiki_mcp.brain import read_secret, write_secret

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"  # only files this app created
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
KEEP_SCOPE = "https://www.googleapis.com/auth/keep"
KEEP_READONLY_SCOPE = "https://www.googleapis.com/auth/keep.readonly"

GOOGLE_CLIENT = "google_client"  # {"client_id", "client_secret"}
GOOGLE_TOKEN = "google_token"  # noqa: S105 - a file name: {"refresh_token", "email", "scope"}
KEEP_ACCOUNT = "google_keep_service_account"  # the service account's JSON key, as downloaded

FLOW_TTL_S = 600
ATTEMPTS = 5
MAX_BACKOFF_S = 60.0


class GoogleError(Exception):
    """A Google call failed; the message is safe to show (never a token)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status of a refused API call


def parse_client(text: str) -> tuple[str, str]:
    """(client id, secret) from the JSON Google Cloud downloads for an OAuth client."""
    try:
        data = json.loads(text)
        app = data.get("web") or data.get("installed")
        client_id, secret = str(app["client_id"]).strip(), str(app["client_secret"]).strip()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("that is not an OAuth client JSON file from Google Cloud") from exc
    check_client(client_id, secret)
    return client_id, secret


def check_client(client_id: str, secret: str) -> None:
    if not client_id.endswith(".apps.googleusercontent.com") or len(client_id) > 200:
        raise ValueError("the client id must end in .apps.googleusercontent.com")
    if not secret or len(secret) > 200 or any(c.isspace() for c in secret):
        raise ValueError("the client secret is missing or malformed")


def parse_service_account(text: str) -> dict[str, Any]:
    """A service account's JSON key, checked before it is stored."""
    try:
        data = json.loads(text)
        ok = (
            data.get("type") == "service_account"
            and "@" in str(data["client_email"])
            and "PRIVATE KEY" in str(data["private_key"])
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("that is not a service account JSON key") from exc
    if not ok:
        raise ValueError("that is not a service account JSON key")
    return dict(data)


def _error_text(response: httpx2.Response) -> str:
    try:
        error = response.json().get("error")
        text = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        text = None
    return f"HTTP {response.status_code}" + (f": {str(text)[:200]}" if text else "")


class Google:
    def __init__(self, secrets_dir: Path, *, transport: httpx2.AsyncBaseTransport | None = None):
        self.secrets_dir = secrets_dir
        self.http = httpx2.AsyncClient(timeout=30.0, transport=transport, follow_redirects=False)
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # tests skip the waits
        self._flows: dict[str, tuple[str, float]] = {}  # state -> (PKCE verifier, expires)
        self._tokens: dict[str, tuple[str, float]] = {}  # "user" or keep key -> (token, expires)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------ stored credentials

    def _json(self, name: str) -> dict[str, Any] | None:
        text = read_secret(self.secrets_dir, name)
        try:
            data = json.loads(text) if text else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def client(self) -> tuple[str, str] | None:
        data = self._json(GOOGLE_CLIENT) or {}
        if data.get("client_id") and data.get("client_secret"):
            return str(data["client_id"]), str(data["client_secret"])
        return None

    def set_client(self, client_id: str, secret: str) -> None:
        check_client(client_id, secret)
        old = self.client()
        write_secret(
            self.secrets_dir,
            GOOGLE_CLIENT,
            json.dumps({"client_id": client_id, "client_secret": secret}),
        )
        if old is None or old[0] != client_id:  # a refresh token belongs to one client
            self._forget()

    def account(self) -> dict[str, str] | None:
        """The connected account: {"email", "scope"} (never the token itself)."""
        data = self._json(GOOGLE_TOKEN)
        if not data or not data.get("refresh_token"):
            return None
        return {"email": str(data.get("email", "")), "scope": str(data.get("scope", ""))}

    def has_scope(self, scope: str) -> bool:
        account = self.account()
        return account is not None and scope in account["scope"].split()

    def keep_account(self) -> dict[str, Any] | None:
        return self._json(KEEP_ACCOUNT)

    def set_keep_account(self, text: str) -> None:
        write_secret(self.secrets_dir, KEEP_ACCOUNT, json.dumps(parse_service_account(text)))
        self._tokens = {k: v for k, v in self._tokens.items() if k == "user"}

    def _forget(self) -> None:
        (self.secrets_dir / GOOGLE_TOKEN).unlink(missing_ok=True)
        self._tokens.pop("user", None)

    # ------------------------------------------------------------ the sign-in flow

    def authorize_url(self, redirect_uri: str, scopes: list[str]) -> str:
        """Google's consent page for `scopes`; the state is good for one callback within
        ten minutes, and only the PKCE verifier kept here can redeem its code."""
        client = self.client()
        if client is None:
            raise GoogleError("enter the OAuth client id and secret first")
        now = time.monotonic()
        self._flows = {s: f for s, f in self._flows.items() if f[1] > now}
        if len(self._flows) >= 20:
            self._flows.clear()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        self._flows[state] = (verifier, now + FLOW_TTL_S)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = {
            "client_id": client[0],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(["openid", "email", *scopes]),
            "state": state,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "access_type": "offline",  # a refresh token
            "prompt": "consent",  # ... also when this account connected before
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    async def finish(self, state: str, code: str, redirect_uri: str) -> str:
        """Redeem the callback's code; returns the connected account's email."""
        flow = self._flows.pop(state, None) if state else None
        client = self.client()
        if flow is None or flow[1] < time.monotonic() or client is None:
            raise GoogleError("this sign-in link expired or was already used: start again")
        if not code:
            raise GoogleError("Google did not return a code: start again")
        data = await self._token_call(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": flow[0],
                "redirect_uri": redirect_uri,
                "client_id": client[0],
                "client_secret": client[1],
            }
        )
        if not data.get("refresh_token"):
            raise GoogleError("Google sent no refresh token: start again")
        email = ""
        try:  # straight from Google's token endpoint over TLS (OIDC Core 3.1.3.7)
            payload = str(data.get("id_token", "")).split(".")[1]
            email = str(json.loads(base64.urlsafe_b64decode(payload + "==")).get("email", ""))
        except (IndexError, ValueError):
            pass
        write_secret(
            self.secrets_dir,
            GOOGLE_TOKEN,
            json.dumps(
                {
                    "refresh_token": data["refresh_token"],
                    "email": email,
                    "scope": str(data.get("scope", "")),
                }
            ),
        )
        self._tokens["user"] = (str(data["access_token"]), self._expiry(data))
        return email

    async def disconnect(self) -> None:
        """Revoke the grant at Google (best effort) and forget the token."""
        data = self._json(GOOGLE_TOKEN) or {}
        if data.get("refresh_token"):
            with contextlib.suppress(httpx2.HTTPError):
                await self.http.post(REVOKE_URL, data={"token": data["refresh_token"]})
        self._forget()

    # ------------------------------------------------------------ tokens

    @staticmethod
    def _expiry(data: dict[str, Any]) -> float:
        try:
            seconds = float(data.get("expires_in", 3600))
        except (TypeError, ValueError):
            seconds = 3600.0
        return time.monotonic() + seconds - 60

    async def _token_call(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            r = await self.http.post(TOKEN_URL, data=form)
        except httpx2.HTTPError as exc:
            raise GoogleError(f"cannot reach Google: {type(exc).__name__}") from exc
        if r.status_code != 200:
            if "invalid_grant" in r.text:
                raise GoogleError("Google refused the sign-in (revoked or expired): connect again")
            raise GoogleError(f"Google token endpoint: {_error_text(r)}")
        data = r.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise GoogleError("Google token endpoint: no access token")
        return data

    async def _access_token(self, keep: tuple[str, str] | None, fresh: bool) -> str:
        key = f"keep {keep[0]} {keep[1]}" if keep else "user"
        cached = self._tokens.get(key)
        if cached and not fresh and cached[1] > time.monotonic():
            return cached[0]
        if keep is None:
            client, stored = self.client(), self._json(GOOGLE_TOKEN) or {}
            if client is None or not stored.get("refresh_token"):
                raise GoogleError("Google is not connected: use Connect Google in the setup page")
            form = {
                "grant_type": "refresh_token",
                "refresh_token": str(stored["refresh_token"]),
                "client_id": client[0],
                "client_secret": client[1],
            }
        else:
            account = self.keep_account()
            if account is None:
                raise GoogleError("no Keep service account key uploaded")
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": account["client_email"],
                    "sub": keep[0],  # the Workspace user whose notes are read
                    "scope": keep[1],
                    "aud": TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
                account["private_key"],
                algorithm="RS256",
                headers={"kid": str(account.get("private_key_id", ""))},
            )
            form = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        data = await self._token_call(form)
        self._tokens[key] = (str(data["access_token"]), self._expiry(data))
        return str(data["access_token"])

    # ------------------------------------------------------------ API calls

    async def request(
        self, method: str, url: str, *, keep: tuple[str, str] | None = None, **kwargs: Any
    ) -> httpx2.Response:
        """An authorized call (as the connected user, or with `keep` = (user, scope) as the
        Keep service account). Raises GoogleError when it finally fails."""
        extra = kwargs.pop("headers", {})
        fresh = False
        r: httpx2.Response | None = None
        for attempt in range(ATTEMPTS):
            token = await self._access_token(keep, fresh)
            headers = {**extra, "Authorization": f"Bearer {token}"}
            try:
                r = await self.http.request(method, url, headers=headers, **kwargs)
            except httpx2.HTTPError as exc:
                if attempt == ATTEMPTS - 1:
                    raise GoogleError(f"cannot reach Google: {type(exc).__name__}") from exc
                await self.sleep(min(2.0**attempt, MAX_BACKOFF_S))
                continue
            if r.status_code == 401 and not fresh:
                fresh = True  # the access token expired early or was revoked: refresh once
                continue
            fresh = False
            limited = r.status_code == 403 and "ateLimitExceeded" in r.text
            if r.status_code == 429 or r.status_code >= 500 or limited:
                retry_after = r.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else 2.0**attempt
                await self.sleep(min(delay, MAX_BACKOFF_S))
                continue
            if r.is_error:
                raise GoogleError(_error_text(r), r.status_code)
            return r
        raise GoogleError(_error_text(r) if r is not None else "no response")

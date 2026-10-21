"""Endpoint dashboard: every URL of the stack, computed like scripts/lib/modes.sh, with
its status.

Two kinds of checks, run concurrently with short timeouts and cached briefly:
  - internal: the service answering on the Docker network (always meaningful);
  - public:   the URL as the outside world sees it, from inside this container.
    Tailnet names usually do not resolve inside Docker, so those show "unknown"
    rather than a false "down"; `make access-status` on the host checks them fully.
Only URLs from the configuration are ever requested (no user input reaches a probe).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx2

from portal.config import Settings

State = Literal["up", "down", "unknown"]


@dataclass(frozen=True)
class Status:
    state: State
    detail: str


@dataclass(frozen=True)
class Probe:
    key: str
    url: str
    judge: Callable[[httpx2.Response], Status]
    public: bool = False


@dataclass
class Link:
    label: str
    url: str
    copy: bool = False
    open: bool = True


@dataclass
class Endpoint:
    title: str
    description: str
    links: list[Link]
    probes: list[str] = field(default_factory=list)


def _ok_2xx(r: httpx2.Response) -> Status:
    if 200 <= r.status_code < 300:
        return Status("up", f"HTTP {r.status_code}")
    return Status("down", f"HTTP {r.status_code}")


def _ui_public(r: httpx2.Response) -> Status:
    location = r.headers.get("location", "")
    if r.status_code in (301, 302, 303, 307, 308) and "cloudflareaccess.com" in location:
        return Status("up", "behind Cloudflare Access (login required)")
    return _ok_2xx(r)


def _expect_json(key: str, expected: str) -> Callable[[httpx2.Response], Status]:
    def judge(r: httpx2.Response) -> Status:
        if r.status_code != 200:
            return Status("down", f"HTTP {r.status_code}")
        try:
            value = r.json().get(key)
        except ValueError:
            return Status("down", "not JSON (another service answers here?)")
        if value != expected:
            return Status("down", f"{key} is {value!r}, expected {expected!r}")
        return Status("up", f"{key} matches")

    return judge


class Dashboard:
    def __init__(
        self,
        settings: Settings,
        *,
        cache_s: float = 15.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.s = settings
        self.cache_s = cache_s
        self.transport = transport
        self._cache: dict[str, Status] = {}
        self._checked_at = -1e9
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ what exists

    @property
    def mcp_url(self) -> str | None:
        return f"{self.s.mcp_base_url}/mcp" if self.s.mcp_base_url else None

    def probes(self) -> list[Probe]:
        s = self.s
        probes = [
            Probe("librechat", f"{s.librechat_url}/health", _ok_2xx),
            Probe("litellm", f"{s.litellm_url}/health/liveliness", _ok_2xx),
            Probe("wiki_internal", f"{s.wiki_mcp_internal_url}/health", _ok_2xx),
            Probe("ui_public", s.ui_url + "/", _ui_public, public=True),
        ]
        base = s.mcp_base_url
        if base:
            probes += [
                Probe("wiki_public", f"{s.wiki_mcp_public_url}/health", _ok_2xx),
                Probe(
                    "mcp_prm",
                    f"{base}/.well-known/oauth-protected-resource/mcp",
                    _expect_json("resource", f"{base}/mcp"),
                    public=True,
                ),
                Probe(
                    "mcp_as",
                    f"{base}/.well-known/oauth-authorization-server",
                    _expect_json("issuer", base),
                    public=True,
                ),
                Probe("mcp_health", f"{base}/health", _ok_2xx, public=True),
            ]
        return probes

    def endpoints(self) -> list[Endpoint]:
        s = self.s
        out = [
            Endpoint(
                "Chat (LibreChat)",
                f"Your chat UI. Access mode: {s.ui_access_mode.value}.",
                [Link("Open", s.ui_url)],
                ["ui_public", "librechat"],
            ),
            Endpoint(
                "Portal",
                f"This site. Same audience as the chat UI ({s.ui_access_mode.value}).",
                [Link("Portal URL", s.origin, copy=True, open=False)],
            ),
        ]
        base = s.mcp_base_url
        if base:
            out += [
                Endpoint(
                    "MCP connector",
                    "Add this URL as a custom connector in Claude.ai, ChatGPT or Le Chat "
                    f"(exposure mode: {s.mcp_exposure_mode.value}). OAuth protects it.",
                    [Link("Connector URL", f"{base}/mcp", copy=True, open=False)],
                    ["mcp_prm", "wiki_public"],
                ),
                Endpoint(
                    "OAuth discovery",
                    "What the AI apps read first to find the login.",
                    [
                        Link(
                            "Protected resource metadata",
                            f"{base}/.well-known/oauth-protected-resource/mcp",
                        ),
                        Link(
                            "Authorization server metadata",
                            f"{base}/.well-known/oauth-authorization-server",
                        ),
                    ],
                    ["mcp_as"],
                ),
                Endpoint(
                    "Health",
                    "Public health check of the MCP server.",
                    [Link("Health", f"{base}/health")],
                    ["mcp_health"],
                ),
            ]
        else:
            out.append(
                Endpoint(
                    "MCP connector",
                    "MCP_EXPOSURE_MODE=none: no public endpoint. Only LibreChat uses the wiki.",
                    [],
                )
            )
        return out

    def services(self) -> list[tuple[str, str, str]]:
        """(label, key, where) for the internal service table."""
        rows = [
            ("LibreChat", "librechat", "librechat:3080"),
            ("LiteLLM gateway", "litellm", "litellm:4000"),
            ("wiki-mcp internal listener", "wiki_internal", "wiki-mcp:8081"),
        ]
        if self.s.mcp_base_url:
            rows.append(("wiki-mcp public listener", "wiki_public", "wiki-mcp:8080"))
        return rows

    # ------------------------------------------------------------------ checks

    async def statuses(self, *, refresh: bool = False) -> tuple[dict[str, Status], float]:
        async with self._lock:
            age = time.monotonic() - self._checked_at
            if refresh or age > self.cache_s:
                self._cache = await self._run_all()
                self._checked_at = time.monotonic()
            return self._cache, time.monotonic() - self._checked_at

    async def _run_all(self) -> dict[str, Status]:
        async with httpx2.AsyncClient(
            follow_redirects=False, transport=self.transport, headers={"User-Agent": "sb-portal"}
        ) as http:
            probes = self.probes()
            results = await asyncio.gather(*(self._run(http, p) for p in probes))
        return {p.key: r for p, r in zip(probes, results, strict=True)}

    async def _run(self, http: httpx2.AsyncClient, probe: Probe) -> Status:
        timeout = 5.0 if probe.public else 3.0
        try:
            r = await http.get(probe.url, timeout=timeout)
        except httpx2.ConnectError as exc:
            if probe.public and _is_dns(exc):
                return Status("unknown", "name not resolvable from inside Docker")
            return Status("down", "connection refused or unreachable")
        except httpx2.TimeoutException:
            return Status("down", "timed out")
        except httpx2.HTTPError as exc:
            return Status("down", type(exc).__name__)
        return probe.judge(r)


def _is_dns(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(s in text for s in ("name or service", "nodename", "name resolution", "getaddrinfo"))

"""Process wiring: build the store, the core listeners (internal, local) and the supervisor that
starts the mode's adapters (public MCP listener, Drive export, inbox, the folder mode's export
and inbox), run them, shut down cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.types import ASGIApp

from wiki_mcp.audit import AuditLog
from wiki_mcp.brain import INTERNAL_TOKEN, PORTAL_TOKEN, ensure_token, load_config
from wiki_mcp.config import Settings
from wiki_mcp.context import ensure_context
from wiki_mcp.dream.sessions import DreamSessions
from wiki_mcp.internal import build_internal_app
from wiki_mcp.mcp.tools import ToolDeps
from wiki_mcp.watcher import WikiWatcher
from wiki_mcp.wiki.index import SearchIndex
from wiki_mcp.wiki.maintenance import nightly_loop
from wiki_mcp.wiki.push import DebouncedPusher
from wiki_mcp.wiki.store import WikiStore
from wiki_mcp.wiki.types import TypeRegistry

log = logging.getLogger("wiki_mcp")


@dataclass
class Runtime:
    settings: Settings
    registry: TypeRegistry
    index: SearchIndex
    store: WikiStore
    audit: AuditLog
    sessions: DreamSessions
    watcher: WikiWatcher
    pusher: DebouncedPusher | None = None
    background: list[asyncio.Task[Any]] = field(default_factory=list)

    @property
    def deps(self) -> ToolDeps:
        return ToolDeps(self.store, self.audit, self.settings.stale_after_days, self.sessions)

    async def close(self) -> None:
        for task in self.background:
            task.cancel()
        if self.pusher is not None:
            await self.pusher.flush()
        self.index.close()
        self.audit.close()
        self.sessions.close()


async def create_runtime(settings: Settings, *, run_background: bool = True) -> Runtime:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    registry = TypeRegistry.load(settings.types_file)
    index = SearchIndex(settings.state_dir / "search.sqlite")
    audit = AuditLog(settings.state_dir / "audit.sqlite")
    store = WikiStore(
        settings.wiki_dir,
        index,
        registry,
        max_file_bytes=settings.max_file_bytes,
        max_source_bytes=settings.max_source_bytes,
        wiki_title=settings.wiki_title,
        public_base_url=settings.public_base_url,
        proposal_threshold=settings.proposal_threshold(),
    )
    sessions = DreamSessions(settings.state_dir / "dreams.sqlite")  # assistant dreams
    watcher = WikiWatcher(store, settings.watch_debounce_ms)
    rt = Runtime(settings, registry, index, store, audit, sessions, watcher)
    if settings.git_remote_url:
        rt.pusher = DebouncedPusher(
            store.repo,
            settings.git_remote_url,
            settings.git_remote_branch,
            delay=settings.git_push_debounce_s,
            ssh_key=settings.git_ssh_key_file,
        )
        store.on_commit = rt.pusher.notify
    seed = settings.seed_dir if settings.seed_dir.is_dir() else None
    await store.startup(seed)
    if seed is not None:
        await ensure_context(store, seed)
    log.info("wiki ready: %d documents indexed", len(index.all()))
    if run_background:
        rt.background.append(asyncio.create_task(watcher.run()))
    if run_background and settings.lint_schedule:
        rt.background.append(
            asyncio.create_task(
                nightly_loop(store, settings.lint_schedule, settings.stale_after_days)
            )
        )
    return rt


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to us (two servers share one loop)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def uvicorn_server(app: ASGIApp, host: str, port: int, level: str) -> _Server:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        proxy_headers=False,  # client IPs are derived by wiki_mcp.net from trusted proxies only
        server_header=False,
        log_level=level.lower(),
        access_log=False,
        timeout_graceful_shutdown=10,
    )
    return _Server(config)


async def serve(settings: Settings) -> None:
    from wiki_mcp.folder import FolderExporter, FolderInboxPoller
    from wiki_mcp.gdrive import DriveExporter
    from wiki_mcp.google import Google
    from wiki_mcp.inbox import InboxPoller
    from wiki_mcp.local import build_local_app
    from wiki_mcp.public import McpPublicAdapter
    from wiki_mcp.supervisor import Supervisor

    config = load_config(settings.config_file)  # a broken config.yaml fails before anything runs
    rt = await create_runtime(settings)
    # Tokens between the portal, local clients and the brain: from the environment, else
    # generated once in the shared secrets volume.
    secrets_dir = settings.secrets_dir
    internal_token = settings.internal_bearer_token.get_secret_value() or ensure_token(
        secrets_dir, INTERNAL_TOKEN
    )
    portal_token = settings.portal_bearer_token.get_secret_value() or ensure_token(
        secrets_dir, PORTAL_TOKEN
    )
    servers: list[_Server] = []
    if len(internal_token) < 32:
        log.warning(
            "INTERNAL_BEARER_TOKEN missing or shorter than 32 chars: internal listener disabled"
        )
    else:
        if len(portal_token) < 32:
            log.warning("PORTAL_BEARER_TOKEN shorter than 32 chars: portal API disabled")
            portal_token = ""
        internal_app, _ = build_internal_app(
            rt.deps,
            token=internal_token,
            client_name=settings.internal_client_name,
            scopes=settings.internal_scopes,
            allowed_hosts=settings.internal_allowed_hosts,
            portal_token=portal_token,
            oauth_db=settings.state_dir / "oauth.sqlite",
        )
        servers.append(
            uvicorn_server(
                internal_app, settings.internal_host, settings.internal_port, settings.log_level
            )
        )

    google = Google(secrets_dir)
    mcp = McpPublicAdapter(rt)
    supervisor = Supervisor(
        [
            mcp,
            DriveExporter(rt, google),
            InboxPoller(rt, google),
            FolderExporter(rt),
            FolderInboxPoller(rt),
        ]
    )
    rt.background.append(asyncio.create_task(mcp.follow(supervisor)))
    local_app = build_local_app(rt, supervisor, api_token=internal_token, google=google)
    servers.append(
        uvicorn_server(local_app, settings.local_host, settings.local_port, settings.log_level)
    )
    if config is None:
        log.info("setup needed: open %s", settings.setup_url)
    await supervisor.apply(config)

    loop = asyncio.get_running_loop()

    def stop() -> None:
        log.info("shutdown requested")
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        await supervisor.close()
        await google.aclose()
        await rt.close()
        log.info("stopped")

"""Adapters started and stopped inside the brain process by the saved mode, without a restart.

The core (wiki, index, REST API, setup wizard, portal API) always runs. An adapter is an
optional way in or out of the brain that only some modes need: the public MCP listener for
mcp/both (public.McpPublicAdapter); the Drive export (gdrive.DriveExporter) and the inbox
(inbox.InboxPoller) for gdrive/both; the folder export and import (folder.FolderExporter,
folder.FolderInboxPoller) for folder. Saving new settings restarts only the adapters whose mode or
config section changed; new credentials (Connect Google) restart the adapters that use them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from wiki_mcp.brain import BrainConfig, Mode

log = logging.getLogger(__name__)


class NotReady(Exception):
    """An adapter cannot run yet (a setting is missing): a warning, retried at the next apply."""


class Adapter(Protocol):
    name: str
    modes: frozenset[Mode]  # runs when the saved mode is one of these
    section: str  # the BrainConfig section it reads: a change there restarts it

    async def start(self, config: BrainConfig) -> None: ...

    async def stop(self) -> None: ...


def wanted(config: BrainConfig | None, adapters: Iterable[Adapter]) -> dict[str, object]:
    """The adapters the config needs, each with the settings it runs with (None: setup state,
    nothing runs)."""
    if config is None:
        return {}
    return {a.name: getattr(config, a.section) for a in adapters if config.mode in a.modes}


def plan(running: Mapping[str, object], want: Mapping[str, object]) -> tuple[list[str], list[str]]:
    """(to stop, to start): an adapter whose settings changed is in both."""
    stop = [n for n, cfg in running.items() if n not in want or want[n] != cfg]
    start = [n for n, cfg in want.items() if n not in running or running[n] != cfg]
    return stop, start


class Supervisor:
    def __init__(self, adapters: Iterable[Adapter]) -> None:
        self.adapters = {a.name: a for a in adapters}
        self.running: dict[str, object] = {}
        self.config: BrainConfig | None = None
        self.problems: dict[str, str] = {}  # why a wanted adapter is not running
        self._lock = asyncio.Lock()

    async def apply(self, config: BrainConfig | None) -> None:
        async with self._lock:
            self.config = config
            stop, start = plan(self.running, wanted(config, self.adapters.values()))
            for name in stop:
                log.info("stopping adapter %s", name)
                del self.running[name]
                try:
                    await self.adapters[name].stop()
                except Exception:
                    log.exception("adapter %s failed to stop", name)
            self.problems = {}
            for name in start:
                assert config is not None
                log.info("starting adapter %s", name)
                try:
                    await self.adapters[name].start(config)
                except NotReady as exc:
                    log.warning("adapter %s not started: %s", name, exc)
                    self.problems[name] = str(exc)
                    continue
                except (Exception, SystemExit):  # uvicorn exits on a port it cannot bind
                    log.exception("adapter %s failed to start", name)
                    self.problems[name] = "failed to start: see the brain's log"
                    continue
                self.running[name] = getattr(config, self.adapters[name].section)

    async def restart(self, section: str) -> None:
        """Restart the running adapters that read `section` and retry those not running."""
        async with self._lock:
            for name in [n for n in self.running if self.adapters[n].section == section]:
                del self.running[name]
                try:
                    await self.adapters[name].stop()
                except Exception:
                    log.exception("adapter %s failed to stop", name)
        await self.apply(self.config)

    async def close(self) -> None:
        await self.apply(None)

    def status(self) -> dict[str, bool]:
        return {name: name in self.running for name in self.adapters}

    def details(self) -> dict[str, dict[str, Any]]:
        """Per adapter: why it is not running, and the last run's report of those that sync."""
        out: dict[str, dict[str, Any]] = {}
        for name, adapter in self.adapters.items():
            info = dict(getattr(adapter, "report", None) or {}) if name in self.running else {}
            if name in self.problems:
                info["problem"] = self.problems[name]
            if info:
                out[name] = info
        return out

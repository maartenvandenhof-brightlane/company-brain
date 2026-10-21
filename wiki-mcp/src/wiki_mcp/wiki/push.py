"""Debounced `git push` to an optional backup remote."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from wiki_mcp.wiki.gitrepo import GitError, GitRepo

log = logging.getLogger(__name__)


class DebouncedPusher:
    """Pushes at most once per `delay` seconds after the last commit."""

    def __init__(
        self,
        repo: GitRepo,
        remote_url: str,
        branch: str = "main",
        *,
        delay: float = 60.0,
        ssh_key: Path | None = None,
    ) -> None:
        self.repo = repo
        self.remote_url = remote_url
        self.branch = branch
        self.delay = delay
        self.ssh_key = ssh_key
        self._task: asyncio.Task[None] | None = None
        self._pending = False
        self.pushes = 0
        self.last_error: str | None = None

    async def notify(self, _sha: str) -> None:
        self._pending = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._pending:
            await asyncio.sleep(self.delay)
            self._pending = False
            await self.push_now()

    async def push_now(self) -> bool:
        try:
            await asyncio.to_thread(
                self.repo.push, self.remote_url, self.branch, ssh_key=self.ssh_key
            )
        except (GitError, OSError) as exc:
            # Never log the remote URL: it may embed a token.
            self.last_error = str(exc).replace(self.remote_url, "<remote>")
            log.warning("wiki backup push failed: %s", self.last_error)
            return False
        self.pushes += 1
        self.last_error = None
        return True

    async def flush(self) -> None:
        """Called on shutdown: push immediately if a push is pending."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._pending = False
            await self.push_now()

"""Identity of whoever performs a wiki operation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from wiki_mcp.config import Trust

_CLIENT = re.compile(r"[^a-z0-9._-]+")
# Pages marked `private: true` are visible only to actors holding this scope.
PRIVATE_SCOPE = "wiki:private"


def client_slug(name: str) -> str:
    return _CLIENT.sub("-", name.lower()).strip("-")[:60] or "unknown"


@dataclass(frozen=True)
class Actor:
    """A calling client. `trust` is the highest trust it may claim for writes."""

    client: str
    trust: Trust = Trust.AGENT
    scopes: frozenset[str] = frozenset({"wiki:read", "wiki:write"})
    human: str | None = None  # set when a human decision is recorded (accept/reject proposal)

    @property
    def slug(self) -> str:
        return client_slug(self.client)

    @property
    def git_author(self) -> tuple[str, str]:
        return self.client, f"{self.slug}@clients.wiki.local"

    @property
    def generated_by(self) -> str:
        """OKF §7 actor string for `generated.by`."""
        if self.client.startswith("process:"):
            return self.client
        return f"{self.slug}/wiki-mcp"

    def can(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def sees_private(self) -> bool:
        return PRIVATE_SCOPE in self.scopes


SYSTEM = Actor(
    "wiki-mcp", Trust.OWNER, frozenset({"wiki:read", "wiki:write", "wiki:admin", PRIVATE_SCOPE})
)
MAINTENANCE = Actor("process:nightly-lint", Trust.AGENT, frozenset({"wiki:read", "wiki:write"}))
# The portal's dreams (background consolidation): hygiene fixes, the briefing, the dream log.
DREAM = Actor("process:dream", Trust.AGENT, frozenset({"wiki:read", "wiki:write"}))

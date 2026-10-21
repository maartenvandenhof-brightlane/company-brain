"""Server-side sessions.

The browser only holds a random session id in an HttpOnly, Secure, SameSite=Strict
cookie; everything else (who is logged in, when the password was last entered, the
CSRF token) stays in memory here. A restart signs everyone out, which is fine for a
single-owner portal and means no session secret has to be stored anywhere.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

COOKIE_SECURE = "__Host-sb_portal"  # __Host-: Secure, Path=/, no Domain (no subdomain can set it)
COOKIE_PLAIN = "sb_portal"  # loopback development over http only


@dataclass
class Session:
    id: str
    csrf: str
    created: float
    seen: float
    user: str | None = None
    auth_at: float = 0.0
    notices: list[tuple[str, str]] = field(default_factory=list)  # (ok|bad, text)

    @property
    def authenticated(self) -> bool:
        return self.user is not None

    def recently_authenticated(self, window: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.authenticated and now - self.auth_at <= window

    def notify(self, text: str, *, ok: bool = True) -> None:
        self.notices.append(("ok" if ok else "bad", text))

    def pop_notices(self) -> list[tuple[str, str]]:
        out, self.notices = self.notices, []
        return out


class SessionStore:
    def __init__(self, idle_s: float, max_s: float, max_sessions: int = 500) -> None:
        self.idle_s = idle_s
        self.max_s = max_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}

    def new(self, now: float | None = None) -> Session:
        now = time.monotonic() if now is None else now
        if len(self._sessions) >= self.max_sessions:
            self._prune(now)
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now)
        self._sessions[session.id] = session
        return session

    def get(
        self, sid: str | None, now: float | None = None, *, touch: bool = True
    ) -> Session | None:
        """The live session with this id. touch=False looks without counting as activity
        (background refreshes must not keep an idle session alive)."""
        if not sid or len(sid) > 100:
            return None
        now = time.monotonic() if now is None else now
        session = self._sessions.get(sid)
        if session is None:
            return None
        if now - session.seen > self.idle_s or now - session.created > self.max_s:
            del self._sessions[sid]
            return None
        if touch:
            session.seen = now
        return session

    def login(self, session: Session, user: str, now: float | None = None) -> Session:
        """Start an authenticated session under a new id and CSRF token (no fixation)."""
        now = time.monotonic() if now is None else now
        self._sessions.pop(session.id, None)
        fresh = Session(
            secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now, user, auth_at=now
        )
        self._sessions[fresh.id] = fresh
        return fresh

    def drop(self, session: Session) -> None:
        self._sessions.pop(session.id, None)

    def drop_all(self) -> None:
        self._sessions.clear()

    def _prune(self, now: float) -> None:
        for sid, s in list(self._sessions.items()):
            if now - s.seen > self.idle_s or now - s.created > self.max_s:
                del self._sessions[sid]
        if len(self._sessions) >= self.max_sessions:
            # Anonymous (login-page) sessions go first, oldest first; never logged-in ones.
            anon = sorted(
                (s for s in self._sessions.values() if not s.authenticated), key=lambda s: s.seen
            )
            for s in anon[: max(1, len(anon) // 2)]:
                del self._sessions[s.id]

    def __len__(self) -> int:
        return len(self._sessions)

"""Structured audit log: every auth event and tool call.

Events go to stdout as one JSON object per line (for `docker compose logs`) and
into an SQLite table in the state volume (for later inspection / retention).
Secrets must never be passed to `event()`; values are truncated defensively.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("wiki_mcp.audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    client TEXT,
    ip TEXT,
    outcome TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

_REDACT_KEYS = {
    "password",
    "token",
    "access_token",
    "refresh_token",
    "code",
    "client_secret",
    "totp",
}


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:300] + "…"
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k in _REDACT_KEYS else _clip(v))
            for k, v in list(value.items())[:30]
        }
    if isinstance(value, list | tuple):
        return [_clip(v) for v in list(value)[:30]]
    return value


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if path is not None:
            self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def event(
        self,
        kind: str,
        *,
        client: str | None = None,
        ip: str | None = None,
        outcome: str = "ok",
        **data: Any,
    ) -> None:
        data = {k: ("[redacted]" if k in _REDACT_KEYS else _clip(v)) for k, v in data.items()}
        record = {"audit": kind, "client": client, "ip": ip, "outcome": outcome, **data}
        _logger.info(json.dumps(record, default=str, ensure_ascii=False))
        if self._db is not None:
            with self._lock:
                self._db.execute(
                    "INSERT INTO audit VALUES (?,?,?,?,?,?)",
                    (time.time(), kind, client, ip, outcome, json.dumps(data, default=str)),
                )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, client, ip, outcome, data FROM audit ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": r[0],
                "kind": r[1],
                "client": r[2],
                "ip": r[3],
                "outcome": r[4],
                **json.loads(r[5]),
            }
            for r in rows
        ]

    def close(self) -> None:
        if self._db is not None:
            self._db.close()

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import subprocess
from pathlib import Path

import pytest

from tests.conftest import person
from wiki_mcp.audit import AuditLog
from wiki_mcp.net import client_ip, parse_networks
from wiki_mcp.wiki.actor import Actor
from wiki_mcp.wiki.maintenance import seconds_until, write_lint_report
from wiki_mcp.wiki.push import DebouncedPusher
from wiki_mcp.wiki.store import WikiStore


async def test_debounced_push(store: WikiStore, agent: Actor, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    pusher = DebouncedPusher(store.repo, str(remote), "main", delay=0.2)
    store.on_commit = pusher.notify
    for i in range(3):
        await store.write_concept(f"wiki/people/p{i}", person(f"P{i}"), "x", "add person", agent)
    await asyncio.sleep(0.6)
    assert pusher.pushes == 1  # three commits, one push
    head = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == store.repo.head()


async def test_push_failure_hides_remote_url(store: WikiStore) -> None:
    secret_url = "https://user:supersecret@invalid.example/repo.git"
    pusher = DebouncedPusher(store.repo, secret_url, "main", delay=0)
    assert await pusher.push_now() is False
    assert pusher.last_error and "supersecret" not in pusher.last_error


async def test_nightly_report_written(store: WikiStore) -> None:
    res = await write_lint_report(store, 180)
    assert res.path.startswith("wiki/maintenance/lint-") and res.status == "committed"
    _, doc, _ = store.read(res.id)
    assert doc.type == "maintenance-report"
    assert doc.frontmatter["lint"]["ok"] is True  # type: ignore[index]
    assert doc.frontmatter["generated"]["by"] == "process:nightly-lint"  # type: ignore[index]
    again = await write_lint_report(store, 180)
    assert again.path == res.path and not again.created


def test_seconds_until() -> None:
    now = dt.datetime(2026, 9, 27, 3, 0, tzinfo=dt.UTC)
    assert seconds_until("03:30", now) == 1800
    assert seconds_until("02:00", now) == 23 * 3600


def _scope(peer: str, headers: dict[str, str]) -> dict[str, object]:
    return {
        "client": (peer, 1234),
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


@pytest.mark.parametrize(
    ("peer", "headers", "expected"),
    [
        ("203.0.113.9", {"x-forwarded-for": "1.2.3.4"}, "203.0.113.9"),  # untrusted peer: ignored
        ("172.30.0.10", {"cf-connecting-ip": "1.2.3.4"}, "1.2.3.4"),
        ("172.30.0.10", {"x-forwarded-for": "9.9.9.9, 1.2.3.4"}, "1.2.3.4"),
        ("172.30.0.10", {"x-forwarded-for": "1.2.3.4, 172.30.0.11"}, "1.2.3.4"),
        ("172.30.0.10", {"x-forwarded-for": "garbage"}, "172.30.0.10"),
        ("172.30.0.10", {}, "172.30.0.10"),
    ],
)
def test_client_ip(peer: str, headers: dict[str, str], expected: str) -> None:
    trusted = parse_networks(["172.30.0.0/24"])
    assert client_ip(_scope(peer, headers), trusted) == expected


def test_audit_redacts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    audit = AuditLog(tmp_path / "a.sqlite")
    with caplog.at_level(logging.INFO, logger="wiki_mcp.audit"):
        audit.event(
            "login", client="x", ip="1.2.3.4", outcome="fail", password="hunter2", note="n" * 1000
        )
    line = json.loads(caplog.records[0].getMessage())
    assert line["password"] == "[redacted]" and len(line["note"]) < 400
    stored = audit.recent()[0]
    assert stored["password"] == "[redacted]" and stored["outcome"] == "fail"
    audit.close()

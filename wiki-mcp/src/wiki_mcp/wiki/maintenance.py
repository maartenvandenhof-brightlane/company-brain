"""Nightly maintenance: run lint and file the report as a dated wiki page (no LLM calls)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import PurePosixPath

from wiki_mcp.wiki.actor import MAINTENANCE
from wiki_mcp.wiki.lint import LintReport, lint_bundle
from wiki_mcp.wiki.okf import now_ts
from wiki_mcp.wiki.store import WikiStore, WriteResult

log = logging.getLogger(__name__)


def run_lint(store: WikiStore, stale_after_days: int, *, private: bool = True) -> LintReport:
    """Lint the bundle. Without `private`, findings about private documents are left out."""
    report = lint_bundle(
        list(store.all_markdown()), store.registry, stale_after_days=stale_after_days
    )
    if not private:
        hidden = store.private_paths()
        report.issues = [i for i in report.issues if i.path not in hidden]
    return report


async def write_lint_report(store: WikiStore, stale_after_days: int) -> WriteResult:
    # The report is an ordinary page every client can read: no findings about private pages.
    report = await asyncio.to_thread(run_lint, store, stale_after_days, private=False)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    ctype = store.registry.get("maintenance-report")
    folder = ctype.folder if ctype else PurePosixPath("wiki/maintenance")
    rel = folder / f"lint-{today}.md"
    counts = report.counts
    frontmatter = {
        "type": "maintenance-report",
        "id": str(rel.with_suffix("")),
        "title": f"Lint report {today}",
        "description": (
            f"Nightly wiki health check: {counts['error']} errors, {counts['warning']} warnings."
        ),
        "tags": ["maintenance", "lint"],
        "status": "stable",
        "generated": {"by": MAINTENANCE.generated_by, "at": now_ts()},
        "trust": MAINTENANCE.trust.value,
        "created": now_ts(),
        "updated": now_ts(),
        "lint": {"ok": report.ok, **counts},
    }
    body = f"# Lint report {today}\n\n" + report.to_markdown()
    return await store.write_managed(
        rel, frontmatter, body, f"Nightly lint: {counts['error']} errors", MAINTENANCE
    )


def seconds_until(hhmm: str, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.UTC)
    hour, minute = (int(x) for x in hhmm.split(":", 1))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def nightly_loop(store: WikiStore, schedule: str, stale_after_days: int) -> None:
    """Run forever; cancel the task to stop."""
    while True:
        await asyncio.sleep(seconds_until(schedule))
        try:
            result = await write_lint_report(store, stale_after_days)
            log.info("nightly lint report written to %s", result.path)
        except Exception:
            log.exception("nightly lint failed")

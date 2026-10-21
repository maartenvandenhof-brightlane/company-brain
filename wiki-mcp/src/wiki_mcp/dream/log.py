"""D6, the dream log: what one dream did, as a wiki page for the owner to review.

  wiki/maintenance/dream-YYYY-MM-DD-N (type maintenance-report)
    header      trigger, start and end, the model of each pass, the cost, the base commit;
                for an assistant dream the client it ran in instead of the cost, how its
                session ended, and the MCP clients whose tool calls did the passes
    Ingestion   chats filed, pages created or updated (as drafts)
    Direct changes   per commit: its sha and pass, then "- page: change: reason"
    Proposals   link, pass, one-line summary (speculative ones marked)
    Flagged     orphans, conflicts, model output dropped by validation and why
    Skipped     passes skipped and why (budget, nothing to do, error)

The passes fill a DreamLog as they go; D6 renders it (the same text a dry run keeps in the
dreams table) and writes it with the dream's last commit. It is written by code only. An
assistant dream keeps its DreamLog in its session (dream/sessions.py) between tool calls.

The passes read private pages too, and their flags and notes can quote them: while the wiki
has any private document, the log is private (wiki-mcp also marks a log that names one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from wiki_mcp.dream.reader import WikiReader


class Run(Protocol):
    """What the log says about the dream itself (the portal's dreams row is one)."""

    @property
    def id(self) -> int: ...

    @property
    def started(self) -> str: ...  # ISO datetime, UTC

    @property
    def trigger(self) -> str: ...

    @property
    def dry_run(self) -> bool: ...

    @property
    def base_sha(self) -> str: ...  # the wiki commit it started from


@dataclass
class Commit:
    sha: str
    pass_: str
    lines: list[str]  # "page: change: reason"


@dataclass
class DreamLog:
    ingestion: list[str] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)  # pass -> model

    def flag(self, pass_: str, text: str) -> None:
        self.flagged.append(f"{pass_}: {text}")

    def skip(self, pass_: str, why: str) -> None:
        self.skipped.append(f"{pass_}: {why}")

    def proposed(self, pid: str, pass_: str, summary: str, speculative: bool = False) -> None:
        mark = " (speculative)" if speculative else ""
        self.proposals.append(f"[{pid}](/{pid}.md) {pass_}: {summary}{mark}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ingestion": self.ingestion,
            "commits": [{"sha": c.sha, "pass": c.pass_, "lines": c.lines} for c in self.commits],
            "proposals": self.proposals,
            "flagged": self.flagged,
            "skipped": self.skipped,
            "models": self.models,
        }

    @classmethod
    def of(cls, data: dict[str, Any]) -> DreamLog:
        """A DreamLog from as_dict()."""
        return cls(
            ingestion=[str(x) for x in data.get("ingestion") or []],
            commits=[
                Commit(str(c["sha"]), str(c["pass"]), [str(x) for x in c.get("lines") or []])
                for c in data.get("commits") or []
            ],
            proposals=[str(x) for x in data.get("proposals") or []],
            flagged=[str(x) for x in data.get("flagged") or []],
            skipped=[str(x) for x in data.get("skipped") or []],
            models={str(k): str(v) for k, v in (data.get("models") or {}).items()},
        )


def page_id(dream: Run, number: int) -> str:
    return f"wiki/maintenance/dream-{dream.started[:10]}-{number}"


def render(
    dream: Run,
    log: DreamLog,
    number: int,
    finished: str,
    cost_eur: float,
    *,
    client: str | None = None,
    ended: str | None = None,
    run_in: str | None = None,
) -> str:
    """The log page's body. `client`: the assistant an assistant dream ran in (shown instead
    of the cost, which is the assistant's); `ended`: how its session ended; `run_in`: the MCP
    clients that called its tools, as they name themselves."""
    trigger = "dry run" if dream.dry_run else dream.trigger
    models = ", ".join(f"{p}: {m}" for p, m in log.models.items()) or "none (no model calls)"
    spent = f"Client: {client}." if client else f"Cost: EUR {cost_eur:.4f}."
    how = f" (ended: {ended})" if ended else ""
    if run_in:
        spent += f" Run in: {run_in}."
    out = [
        f"# Dream {dream.started[:10]} #{number}",
        "",
        f"Dream {dream.id}. Trigger: {trigger}. Started {dream.started}, finished {finished}"
        f"{how}. Models: {models}. {spent} Base commit: `{dream.base_sha or '?'}`.",
        "",
        "## Ingestion",
        "",
        *(_items(log.ingestion) or ["Nothing ingested."]),
        "",
        "## Direct changes",
        "",
    ]
    if not log.commits:
        out.append("No direct changes.")
    for commit in log.commits:
        out += [f"### `{commit.sha[:12] or 'not written (dry run)'}` {commit.pass_}", ""]
        out += _items(commit.lines)
        out.append("")
    out += ["", "## Proposals", "", *(_items(log.proposals) or ["No proposals."]), ""]
    out += ["## Flagged", "", *(_items(log.flagged) or ["Nothing flagged."]), ""]
    out += ["## Skipped", "", *(_items(log.skipped) or ["Nothing skipped."]), ""]
    return "\n".join(out).replace("\n\n\n", "\n\n")


def _items(lines: list[str]) -> list[str]:
    return [f"- {' '.join(line.split())}" for line in lines]


async def operation(wiki: WikiReader, dream: Run, number: int, body: str) -> dict[str, Any]:
    """The write_managed operation writing the log page; private while the wiki has any
    private document."""
    docs = (await wiki.index()).get("docs", [])
    private = any(d.get("private") for d in docs)
    return {
        "op": "write_managed",
        "page": page_id(dream, number),
        "body": body,
        "frontmatter": {
            "title": f"Dream {dream.started[:10]} #{number}",
            "description": f"What dream {dream.id} changed, proposed, flagged and skipped.",
            **({"private": True} if private else {}),
        },
    }

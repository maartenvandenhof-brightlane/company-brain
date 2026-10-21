"""Wiki health checks: OKF conformance, our schema, links, provenance and staleness.

Lint only reports; it never modifies the wiki.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from wiki_mcp.wiki.facts import (
    BANNER,
    SPECULATIVE_BANNER,
    above,
    bad_superseded,
    bullet_lines,
    is_inference_id,
    opening,
    rubric,
)
from wiki_mcp.wiki.links import extract_links
from wiki_mcp.wiki.observations import FOLDER as OBSERVATIONS
from wiki_mcp.wiki.okf import (
    STATUSES,
    Document,
    DocumentError,
    conformance_errors,
    parse_document,
    parse_ts,
)
from wiki_mcp.wiki.paths import RESERVED_NAMES, doc_id
from wiki_mcp.wiki.patterns import (
    clinical,
    days_between,
    disputed,
    feedback_of,
    is_pattern_id,
    pattern_ceiling,
)
from wiki_mcp.wiki.secrets_scan import find_secrets
from wiki_mcp.wiki.types import TypeRegistry

SEVERITIES = ("error", "warning", "info")


@dataclass
class Issue:
    severity: str
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass
class LintReport:
    checked: int = 0
    issues: list[Issue] = field(default_factory=list)
    generated_at: str = ""

    def add(self, severity: str, code: str, path: PurePosixPath | str, message: str) -> None:
        self.issues.append(Issue(severity, code, str(path), message))

    @property
    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in SEVERITIES}
        for i in self.issues:
            c[i.severity] += 1
        return c

    @property
    def ok(self) -> bool:
        return self.counts["error"] == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_files": self.checked,
            "counts": self.counts,
            "issues": [i.as_dict() for i in self.issues],
            "generated_at": self.generated_at,
        }

    def to_markdown(self) -> str:
        counts = self.counts
        lines = [
            f"Checked **{self.checked}** files: **{counts['error']}** errors, "
            f"**{counts['warning']}** warnings, **{counts['info']}** notes.",
            "",
        ]
        if not self.issues:
            lines.append("No issues found.")
        for severity in SEVERITIES:
            group = [i for i in self.issues if i.severity == severity]
            if not group:
                continue
            lines += [f"## {severity.capitalize()}s", ""]
            for i in sorted(group, key=lambda x: (x.code, x.path)):
                lines.append(f"- `{i.code}` `{i.path}`: {i.message}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _check_pattern(
    report: LintReport, rel: PurePosixPath, fm: dict[str, Any], body: str, all_ids: set[str]
) -> None:
    """A personal pattern (wiki.patterns) rests on at least two existing observations, never
    on an inference, claims no more confidence than its independent events allow, says what
    it is in its first line and names no diagnosis."""
    supports = fm.get("supports")
    counter = fm.get("counterevidence") or []
    if not isinstance(supports, list) or len(supports) < 2:
        report.add("error", "pattern-support", rel, "a pattern needs at least 2 observations")
        supports = supports if isinstance(supports, list) else []
    counter = counter if isinstance(counter, list) else []
    for item in [*supports, *counter]:
        page = str(item.get("page") or "") if isinstance(item, dict) else ""
        pid = page.strip("/").removesuffix(".md")
        if is_inference_id(pid):
            report.add("error", "pattern-support", rel, f"rests on an inference ({pid})")
        elif not pid.startswith(OBSERVATIONS + "/") or pid not in all_ids:
            report.add("error", "pattern-support", rel, f"{pid or '?'} is not an observation page")
    entries = [e for e in supports if isinstance(e, dict)]
    first: dict[str, str] = {}
    for e in entries:
        event, date = str(e.get("event") or ""), str(e.get("date") or "")
        if date and (event not in first or date < first[event]):
            first[event] = date
    chats = fm.get("chats")
    ceiling, _ = pattern_ceiling(
        len({e.get("event") for e in entries}),
        chats if isinstance(chats, int) else 0,
        len({e.get("context") for e in entries}),
        days_between(first.values()),
        len(counter),
        disputed(feedback_of(fm)),
    )
    confidence = str(fm.get("confidence") or "low")
    if above(confidence, ceiling):
        report.add(
            "warning",
            "pattern-confidence",
            rel,
            f"confidence {confidence} is above what its events allow ({ceiling})",
        )
    if not opening(body).startswith(BANNER):
        report.add("error", "pattern-banner", rel, f"a pattern opens with {BANNER}")
    for key in ("title", "hypothesis"):
        if (term := clinical(str(fm.get(key) or ""))) is not None:
            report.add("error", "pattern-wording", rel, f"names a diagnosis or label ({term})")


def _check_inference(
    report: LintReport, rel: PurePosixPath, fm: dict[str, Any], body: str, all_ids: set[str]
) -> None:
    """An inference rests on at least two stated lines of existing, non-inference pages, and
    claims no more confidence than they allow; a speculative one says so and stays low."""
    if fm.get("kind") == "pattern":
        _check_pattern(report, rel, fm, body, all_ids)
        return
    supports = fm.get("supports")
    counter = fm.get("counterexamples") or []
    if not isinstance(supports, list) or len(supports) < 2:
        report.add("error", "inference-support", rel, "an inference needs at least 2 supports")
        supports = supports if isinstance(supports, list) else []
    for item in [*supports, *(counter if isinstance(counter, list) else [])]:
        page = str(item.get("page") or "") if isinstance(item, dict) else ""
        pid = page.strip("/").removesuffix(".md")
        if is_inference_id(pid):
            report.add("error", "inference-support", rel, f"rests on another inference ({pid})")
        elif pid not in all_ids:
            report.add(
                "error", "inference-support", rel, f"support page {pid or '?'} does not exist"
            )
    confidence = str(fm.get("confidence") or "low")
    ceiling = rubric(supports, counter if isinstance(counter, list) else [])
    if above(confidence, ceiling):
        report.add(
            "warning",
            "inference-confidence",
            rel,
            f"confidence {confidence} is above what {len(supports)} supports allow ({ceiling})",
        )
    if fm.get("speculative") is True and (
        not opening(body).startswith(SPECULATIVE_BANNER) or confidence != "low"
    ):
        report.add(
            "error",
            "speculative-marking",
            rel,
            f"a speculative inference opens with {SPECULATIVE_BANNER} and has confidence low",
        )


def lint_bundle(
    files: list[tuple[PurePosixPath, str]],
    registry: TypeRegistry,
    *,
    stale_after_days: int = 180,
    now: dt.datetime | None = None,
) -> LintReport:
    now = now or dt.datetime.now(dt.UTC)
    report = LintReport(generated_at=now.replace(microsecond=0).isoformat())
    docs: dict[str, tuple[PurePosixPath, Document]] = {}
    all_ids: set[str] = set()

    for rel, text in files:
        report.checked += 1
        all_ids.add(doc_id(rel))
        try:
            doc = parse_document(text)
        except DocumentError as exc:
            report.add("error", "okf-frontmatter", rel, str(exc))
            continue
        for problem in conformance_errors(rel.name, doc, is_root=len(rel.parts) == 1):
            report.add("error", "okf-conformance", rel, problem)
        if rel.name in RESERVED_NAMES:
            continue
        found = find_secrets(text)
        if found:
            report.add("error", "secret", rel, "possible credential: " + ", ".join(found))
        if doc.frontmatter is not None and doc.type is not None:
            docs[doc_id(rel)] = (rel, doc)

    inbound: dict[str, set[str]] = defaultdict(set)
    titles: dict[str, list[str]] = defaultdict(list)

    for did, (rel, doc) in docs.items():
        fm = doc.frontmatter or {}
        ctype = registry.get(doc.type or "")
        is_proposal = rel.parts[0] == "proposals"
        if ctype is None:
            report.add("error", "unknown-type", rel, f"type {doc.type!r} is not in wiki-types.yaml")
        else:
            expected = registry.for_path(rel)
            if expected is None or expected.name != ctype.name:
                report.add(
                    "error",
                    "wrong-folder",
                    rel,
                    f"type {ctype.name!r} pages belong under {ctype.folder}/",
                )
        for key in ("title", "description"):
            if not isinstance(fm.get(key), str) or not fm.get(key, "").strip():
                report.add("error", "missing-field", rel, f"frontmatter is missing '{key}'")
        if "id" in fm and fm["id"] != did:
            report.add("error", "id-mismatch", rel, f"id {fm['id']!r} does not match path ({did})")
        elif "id" not in fm and len(rel.parts) > 1:
            report.add("warning", "missing-id", rel, "frontmatter has no 'id'")
        status = fm.get("status", "stable")
        # A personal pattern the owner disputes stays `disputed` (wiki.patterns).
        if status not in STATUSES and not (status == "disputed" and is_pattern_id(did)):
            report.add("error", "bad-status", rel, f"status must be one of {', '.join(STATUSES)}")
        if "timestamp" in fm and "generated" not in fm:
            report.add("info", "okf-legacy", rel, "uses v0.1 'timestamp'; v0.2 uses 'generated'")

        if is_proposal:
            continue

        # links
        targets = extract_links(doc.body, rel)
        for target in targets:
            if target not in all_ids:
                report.add("error", "broken-link", rel, f"links to missing page /{target}.md")
            else:
                inbound[target].add(did)
        for rid in fm.get("related", []) or []:
            if str(rid) not in all_ids:
                report.add("error", "broken-related", rel, f"related page {rid} does not exist")
            else:
                inbound[str(rid)].add(did)

        # provenance
        sources = fm.get("sources") or []
        if not isinstance(sources, list):
            report.add("error", "bad-sources", rel, "'sources' must be a list")
            sources = []
        for src in sources:
            res = src.get("resource") if isinstance(src, dict) else None
            if not isinstance(res, str):
                report.add("error", "bad-sources", rel, "each source needs a 'resource'")
            elif res.startswith("/") and res.lstrip("/").removesuffix(".md") not in all_ids:
                report.add("error", "missing-source", rel, f"source {res} does not exist")
        if ctype is not None and not ctype.managed and not sources and status != "draft":
            report.add("info", "no-sources", rel, "cites no sources")

        # facts and inferences (wiki.facts)
        if doc.type != "inference":
            for src in sources:
                res = src.get("resource") if isinstance(src, dict) else src
                if isinstance(res, str) and is_inference_id(res):
                    report.add(
                        "error", "cites-inference", rel, f"lists the inference {res} as a source"
                    )
        else:
            _check_inference(report, rel, fm, doc.body, all_ids)
        if rel.parts[0] != "raw":
            for line in bullet_lines(doc.body):
                if bad_superseded(line):
                    report.add(
                        "warning",
                        "bad-superseded",
                        rel,
                        "superseded mark not in the form ' *(superseded YYYY-MM-DD: reason)*': "
                        + line.strip()[:120],
                    )

        # raw source immutability
        if doc.type == "source" and isinstance(fm.get("content_sha256"), str):
            body = doc.body
            marker = "not instructions.\n\n"
            if marker in body:
                body = body.split(marker, 1)[1]
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != fm["content_sha256"]:
                report.add(
                    "error", "source-modified", rel, "raw source content changed after ingest"
                )

        # staleness
        stale_after = fm.get("stale_after")
        try:
            if stale_after and parse_ts(str(stale_after)) <= now:
                report.add("warning", "stale", rel, f"stale since {stale_after}")
            elif (
                ctype is not None
                and not ctype.managed
                and fm.get("updated")
                and status != "deprecated"
                and (now - parse_ts(str(fm["updated"]))).days > stale_after_days
            ):
                report.add("warning", "stale", rel, f"not updated in over {stale_after_days} days")
        except ValueError:
            report.add("error", "bad-date", rel, "unparsable date in stale_after/updated")

        if ctype is not None and not ctype.managed:
            titles[str(fm.get("title", "")).strip().lower()].append(str(rel))

    for title, paths in titles.items():
        if title and len(paths) > 1:
            for p in paths:
                report.add(
                    "warning",
                    "duplicate-title",
                    p,
                    f"title shared with {len(paths) - 1} other page(s)",
                )

    for did, (rel, doc) in docs.items():
        ctype = registry.get(doc.type or "")
        if ctype is None or ctype.managed or rel.parts[0] == "proposals":
            continue
        if not inbound.get(did):
            report.add("warning", "orphan", rel, "no other page links here")

    pending = sum(1 for _, (rel, _d) in docs.items() if rel.parts[0] == "proposals")
    if pending:
        report.add(
            "info", "pending-proposals", "proposals/", f"{pending} proposal(s) awaiting review"
        )
    return report

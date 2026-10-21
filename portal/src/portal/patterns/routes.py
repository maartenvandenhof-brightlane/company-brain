"""The Patterns page: personal patterns and the evidence behind them (owner only).

  GET  /patterns                   the patterns in force, grouped as an assistant sees them
                                   (strengths, difficulties, others), then those the owner
                                   disputes and, folded away, the retired ones; each with its
                                   hypothesis, confidence and why, independent events, evidence
                                   (quote, source, message, date), counterevidence, alternatives
                                   and the owner's answers. Below them, keeping them current: how
                                   many chats are observed, the backfill, Reassess patterns and
                                   the same in Claude.ai or ChatGPT
  GET  /patterns/status            the backfill's and the patterns run's progress (JSON), which
                                   patterns.js follows while one runs
  POST /patterns/backfill          observe chats without an observation page (limit=, outdated=1:
                                   also those an older extractor observed), in the background
  POST /patterns/backfill/cancel   stop after the chat being observed
  POST /patterns/reassess          a patterns run (DreamJob.reassess): the patterns pass, the log
  POST /patterns/assistant         reassess in Claude.ai or ChatGPT (client=claude.ai|chatgpt):
                                   open a patterns session (dreams/assistant.py, kind patterns);
                                   the assistant runs it with the patterns tools (run-patterns)
  POST /patterns/assistant/{id}/end   end that session: its log is written
  POST /patterns/feedback          the owner's verdict on a pattern (id, verdict: agree | dispute
                                   | correct | unsure, note), one wiki commit

Observing chats in Claude.ai or ChatGPT needs no session: the page says what to type (the
run-observe prompt) and how many chats are pending. Both need an app with private access (the
Connected apps page); the page says when the chosen app has none.

The POSTs are plain forms (CSRF and origin checked by `owner`) that go back to the page (an
answer to the pattern's card; patterns.js sends answers in place). A disabled button says why
next to it. What goes into the audit log is counts and verdicts only, never a hypothesis, quote or page id.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from wiki_mcp.wiki.patterns import VERDICTS, is_pattern_id

from portal.dreams.assistant import CLIENTS, AssistantDreams, Refused, client_name, tell, what
from portal.dreams.job import DreamJob
from portal.dreams.store import PATTERNS, Dream
from portal.patterns.backfill import LIMITS, Backfill
from portal.processing.observe import EXTRACTOR
from portal.sessions import Session
from portal.web import Portal, owner
from portal.wikiclient import NotFound, WikiClient, WikiUnavailable

PAGE = "/patterns"
# The answer buttons (a correction has its own, under the note), and how an answer given reads.
VERDICT_LABELS = {"agree": "Agree", "unsure": "Not sure", "dispute": "Dispute"}
ANSWERED = {
    "agree": "You agreed",
    "dispute": "You disputed this",
    "correct": "You reworded this",
    "unsure": "You were not sure",
}
# The groups the page shows, as get_personal_patterns groups them for an assistant (by valence),
# then those the owner disputes; retired ones are folded away below them.
GROUPS = (
    ("strengths", "Strengths"),
    ("difficulties", "Difficulties"),
    ("other", "Other patterns"),
    ("disputed", "Disputed by you"),
)
_VALENCE_GROUP = {"strength": "strengths", "difficulty": "difficulties"}
# What the page shows of a pattern page's frontmatter (wiki_mcp.wiki.patterns.build), filled in
# for older or hand-edited pages so the template never meets a missing key.
_PATTERN: dict[str, Any] = {
    "hypothesis": "",
    "model_hypothesis": "",
    "contexts": [],
    "span": "",
    "valence": "neutral",
    "status": "stable",
    "confidence": "low",
    "confidence_rationale": "",
    "independent_events": 0,
    "chats": 0,
    "span_days": 0,
    "reassess_after": "",
    "supports": [],
    "counterevidence": [],
    "alternatives": [],
    "feedback": [],
}
_EVIDENCE = dict.fromkeys(
    ("quote", "text", "source", "date", "basis", "kind", "context", "page", "obs", "event"), ""
) | {"msg": 0}
_FEEDBACK = {"at": "", "verdict": "", "note": ""}


def _view(did: str, fm: dict[str, Any]) -> dict[str, Any]:
    view = {**_PATTERN, **fm, "id": did}
    for key in ("supports", "counterevidence"):
        view[key] = [{**_EVIDENCE, **e} for e in view[key] or [] if isinstance(e, dict)]
    view["feedback"] = [{**_FEEDBACK, **f} for f in view["feedback"] or [] if isinstance(f, dict)]
    return view


def _group(p: dict[str, Any]) -> str:
    """Which of GROUPS a pattern in force is shown in."""
    if p.get("status") == "disputed":
        return "disputed"
    return _VALENCE_GROUP.get(str(p.get("valence")), "other")


def _coverage(patterns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The coverage of the last reassessment: the most recently written pattern page's, as
    get_personal_patterns reports it (None without one)."""
    newest = max(patterns, key=lambda p: str(p.get("updated") or ""), default=None)
    cov = newest.get("coverage") if newest else None
    return cov if isinstance(cov, dict) else None


# What to type in the assistant to observe chats (ChatGPT may not show MCP prompts).
OBSERVE_TELL = "Observe my chats for personal patterns"


class PatternRoutes:
    def __init__(
        self,
        portal: Portal,
        wiki: WikiClient,
        backfill: Backfill,
        dreams: DreamJob,
        assistant: AssistantDreams,
    ) -> None:
        self.p = portal
        self.wiki = wiki
        self.backfill = backfill
        self.dreams = dreams
        self.assistant = assistant  # the Dreams page's: patterns sessions are dreams too

    def routes(self) -> list[Route]:
        o = owner(self.p)
        return [
            Route(PAGE, o(self.page)),
            Route(f"{PAGE}/status", o(self.status)),
            Route(f"{PAGE}/backfill", o(self.start_backfill), methods=["POST"]),
            Route(f"{PAGE}/backfill/cancel", o(self.cancel_backfill), methods=["POST"]),
            Route(f"{PAGE}/reassess", o(self.reassess), methods=["POST"]),
            Route(f"{PAGE}/assistant", o(self.start_assistant), methods=["POST"]),
            Route(PAGE + "/assistant/{id:int}/end", o(self.end_assistant), methods=["POST"]),
            Route(f"{PAGE}/feedback", o(self.feedback), methods=["POST"]),
        ]

    @staticmethod
    def _back(anchor: str = "") -> RedirectResponse:
        return RedirectResponse(PAGE + (f"#{anchor}" if anchor else ""), status_code=303)

    def _audit(self, request: Request, kind: str, outcome: str, **data: object) -> None:
        self.p.audit.event(kind, client="portal", ip=self.p.ip(request), outcome=outcome, **data)

    async def _patterns(self) -> list[dict[str, Any]]:
        """Every pattern page's frontmatter (with its id), ordered as get_personal_patterns
        orders them: by independent events, then by how many contexts they span."""
        ids = [
            str(d["id"])
            for d in await self.wiki.docs(type="inference")
            if is_pattern_id(str(d.get("id", "")))
        ]
        docs = await asyncio.gather(*(self._doc(i) for i in ids))
        out = [_view(str(d["id"]), d.get("frontmatter") or {}) for d in docs if d is not None]
        out.sort(
            key=lambda p: (
                -int(p.get("independent_events") or 0),
                -len(p.get("contexts") or []),
                str(p["id"]),
            )
        )
        return out

    async def _doc(self, did: str) -> dict[str, Any] | None:
        try:
            return await self.wiki.doc(did)
        except NotFound:
            return None

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        problem = ""
        patterns: list[dict[str, Any]] = []
        counts: dict[str, int | None] = {"observed": None, "pending": None, "outdated": None}
        try:
            patterns = await self._patterns()
            imported = int((await self.wiki.processing()).get("imported") or 0)
            pending = int((await self.wiki.observations_pending(limit=1)).get("total") or 0)
            older = await self.wiki.observations_pending(limit=1, outdated=EXTRACTOR)
            counts["pending"] = pending
            counts["observed"] = imported - pending
            counts["outdated"] = int(older.get("total") or 0) - pending
        except (WikiUnavailable, ValueError) as exc:
            problem = f"the wiki is not answering ({exc})"
        progress = self.dreams.progress
        reassessing = progress is not None and progress.steps[0] == "Patterns"
        running = self.backfill.running or self.dreams.running
        in_assistant, state = await self._session()
        groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in GROUPS}
        for p in patterns:
            if p.get("status") != "deprecated":
                groups[_group(p)].append(p)
        # Why a button is off, said next to it ("" when it is on).
        can_call = bool(self.p.settings.litellm_master_key.get_secret_value())
        no_key = "" if can_call else "Needs the model key: run make up"
        busy = ""
        if self.backfill.running:
            busy = "Busy observing chats"
        elif self.dreams.running:
            busy = "Busy reassessing patterns" if reassessing else "Busy with a dream or ideas run"
        held = self.dreams.assistant
        in_session = f"A session is open in {client_name(held)}" if held else ""
        return self.p.render(
            request,
            "patterns.html",
            session,
            groups=[(key, title, groups[key]) for key, title in GROUPS],
            retired=[p for p in patterns if p.get("status") == "deprecated"],
            coverage=_coverage(patterns),
            counts=counts,
            problem=problem,
            job=self.backfill.job,
            backfilling=self.backfill.running,
            limits=LIMITS,
            progress=progress if reassessing else None,
            dreaming=self.dreams.running,
            running=running,
            verdicts=VERDICT_LABELS,
            answered=ANSWERED,
            can_call=can_call,
            why_not={
                "observe": no_key or (busy if self.dreams.running else ""),
                "reassess": no_key or busy or in_session,
                "session": busy,
            },
            clients=CLIENTS,
            private=await self._private_access(),
            held=held,
            in_assistant=in_assistant,
            state=state,
            client_name=client_name,
            what=what,
            tell=tell(in_assistant) if in_assistant else "",
            observe_tell=OBSERVE_TELL,
        )

    async def _session(self) -> tuple[Dream | None, dict[str, Any] | None]:
        """The last patterns session in an assistant, its row brought up to date, and its
        state (None when it is not known)."""
        dream = self.dreams.store.last_in_assistant(PATTERNS)
        if dream is None:
            return None, None
        try:
            return dream, await self.assistant.sync(dream)
        except (ValueError, WikiUnavailable):
            return dream, None

    async def _private_access(self) -> dict[str, bool | None]:
        """Per assistant (CLIENTS), whether its connected app sees private pages (None: it has
        not connected, or the wiki did not say)."""
        try:
            apps = (await self.wiki.apps()).get("apps") or []
        except (WikiUnavailable, ValueError):
            apps = []
        known = {str(a.get("label")): bool(a.get("private")) for a in apps}
        return {client: known.get(client) for client in CLIENTS}

    async def status(self, request: Request, session: Session, _: FormData | None) -> Response:
        job = self.backfill.job
        progress = self.dreams.progress
        return JSONResponse(
            {
                "backfill": None
                if job is None
                else {
                    "running": self.backfill.running,
                    "total": job.total,
                    "handled": job.handled,
                    "done": job.done,
                    "failed": job.failed,
                    "cost_eur": job.cost_eur,
                    "error": job.error,
                },
                "dream": progress.as_dict() if progress else None,
                "dreaming": self.dreams.running,
            }
        )

    async def start_backfill(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        assert form is not None
        limit = int(s) if (s := str(form.get("limit") or "")).isdigit() else 0
        outdated = form.get("outdated") == "1"
        if limit not in LIMITS:
            session.notify("Choose how many chats to observe.", ok=False)
        elif not self.p.settings.litellm_master_key.get_secret_value():
            session.notify(
                "Observing needs LITELLM_MASTER_KEY in the portal. Run make up.", ok=False
            )
        elif (why := self.backfill.start(limit, outdated)) is not None:
            session.notify(why, ok=False)
        else:
            self._audit(request, "patterns_backfill", "started", limit=limit, outdated=outdated)
            session.notify("Observing chats in the background.")
        return self._back("observe")

    async def cancel_backfill(
        self, request: Request, session: Session, _: FormData | None
    ) -> Response:
        if self.backfill.cancel():
            session.notify("Stopping after the chat being observed.")
        return self._back("observe")

    async def reassess(self, request: Request, session: Session, _: FormData | None) -> Response:
        if not self.p.settings.litellm_master_key.get_secret_value():
            session.notify("Reassessing needs LITELLM_MASTER_KEY in the portal.", ok=False)
        elif self.backfill.running:
            session.notify("Wait for the backfill to finish first.", ok=False)
        elif not self.dreams.reassess():
            session.notify(
                "A dream or an ideas run is going (or an assistant dream is open): try again "
                "when it is done.",
                ok=False,
            )
        else:
            self._audit(request, "patterns_reassess", "started")
            session.notify("Reassessing your patterns: one model call, within the dreams budget.")
        return self._back("reassess")

    async def start_assistant(
        self, request: Request, session: Session, form: FormData | None
    ) -> Response:
        assert form is not None
        client = str(form.get("client") or "")
        if self.backfill.running:
            session.notify("Wait for the backfill to finish first.", ok=False)
            return self._back("assistant")
        try:
            dream = await self.assistant.start(client, "patterns")
        except Refused as exc:
            session.notify(str(exc), ok=False)
            return self._back("assistant")
        name = client_name(dream)
        if not (await self._private_access()).get(client):
            session.notify(
                f"{what(dream)} is waiting for {name}, which has no private access yet: turn "
                "it on under Connected apps, or the assistant is refused.",
                ok=False,
            )
        else:
            session.notify(f"{what(dream)} is waiting for {name}.")
        return self._back("assistant")

    async def end_assistant(
        self, request: Request, session: Session, _: FormData | None
    ) -> Response:
        dream = self.dreams.store.get(int(request.path_params["id"]))
        if dream is None or dream.trigger != PATTERNS:
            session.notify("That patterns session does not exist.", ok=False)
            return self._back("assistant")
        try:
            await self.assistant.end(dream)
        except Refused as exc:
            session.notify(str(exc), ok=False)
        else:
            session.notify(f"{what(dream)} ended: its log is written.")
        return self._back("assistant")

    async def feedback(self, request: Request, session: Session, form: FormData | None) -> Response:
        """Back to the pattern's card when the answer is recorded (its card says so); to the
        top of the page, where the notice is, when it is not."""
        assert form is not None
        pid = str(form.get("id") or "")
        verdict = str(form.get("verdict") or "")
        note = " ".join(str(form.get("note") or "").split())
        if not is_pattern_id(pid) or verdict not in VERDICTS:
            session.notify("Unknown pattern or choice.", ok=False)
            return self._back()
        if verdict == "correct" and len(note) < 3:
            session.notify(
                "Write the pattern in your own words in the note to save your wording.", ok=False
            )
            return self._back()
        try:
            await self.wiki.pattern_feedback(pid, verdict, note)
        except ValueError as exc:
            session.notify(f"The wiki refused this: {exc}", ok=False)
            self._audit(request, "patterns_feedback", "refused", verdict=verdict)
        except WikiUnavailable:
            session.notify("The wiki is not answering: nothing was changed.", ok=False)
        else:
            self._audit(request, "patterns_feedback", "ok", verdict=verdict)
            session.notify("Your answer is saved on the pattern.")
            return self._back(pid.replace("/", "-"))
        return self._back()

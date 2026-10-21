"""Ideas: validation, the citation gate, scoring and the filters, the store (snooze, the
migration, the taste counter), the prompts, the taste note and the schedule."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx2
import pytest

from portal.ideas import collect, generate, score, taste
from portal.ideas.collect import Context
from portal.ideas.job import IdeasJob, next_run, parse_time
from portal.ideas.store import TASTE_EVERY, Idea, IdeaStore, Taste, fingerprint
from portal.processing.llm import LLM, LLMError
from portal.wikiclient import WikiClient
from tests.conftest import _find, make_budget, priced_models

SHED = "wiki/projects/garden-shed"
DECISION = "wiki/decisions/build-the-shed-from-a-kit"
CHAT = "raw/chats/claude/shed"
INFERRED = "wiki/inferred/prefers-kits"
CTX = Context(
    goals=["- Build a shed this autumn (set 2026-09-01)", "- Learn woodwork (set 2026-09-01)"],
    loops=[{"page": SHED, "line": "- [ ] Order the kit", "date": "2026-09"}],
)


def answer(*candidates: object) -> str:
    return json.dumps({"candidates": list(candidates)})


def cand(kind: str = "follow-up", text: str = "Order the kit.", **extra: object) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text,
        "serves": {"type": "goal", "ref": "G1"},
        "why_now": "The autumn sale ends Friday.",
        "next_step": "Order it online.",
        "cites": [DECISION],
        **extra,
    }


def scored(
    kind: str = "follow-up",
    text: str = "Order the kit.",
    scores: tuple[int, int, int, int] = (4, 4, 4, 4),
    serves: str = "goal",
    cites: list[str] | None = None,
) -> Idea:
    idea = Idea(kind, text, cites or [DECISION], "c" if kind == "challenge" else "")
    idea.serves = {"type": serves, "ref": "G1" if serves == "goal" else "", "text": ""}
    idea.score = dict(zip(score.FIELDS, scores, strict=True))
    idea.score["total"] = sum(scores)
    return idea


# --- validate ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not json", "[]", '{"candidates": "none"}', '{"ideas": []}'])
def test_not_the_contract_at_all(bad: str) -> None:
    with pytest.raises(LLMError):
        generate.validate(bad, CTX, 12)


def test_valid_candidates_of_every_kind() -> None:
    ideas, dropped = generate.validate(
        answer(
            cand(),
            cand(
                "continue", "Which base does the shed need?", serves={"type": "loop", "ref": "l1"}
            ),
            cand("connect", "Jane and the shed share Acme.", cites=[SHED, "/wiki/people/jane.md"]),
            cand(
                "challenge",
                "You assume kits are simpler.",
                counter="They need a base too.",
                serves={"type": "none", "ref": None},
            ),
        ),
        CTX,
        12,
    )
    assert dropped == []
    assert [i.kind for i in ideas] == ["follow-up", "continue", "connect", "challenge"]
    assert ideas[0].serves == {
        "type": "goal",
        "ref": "G1",
        "text": "Build a shed this autumn (set 2026-09-01)",
    }
    assert ideas[1].serves == {"type": "loop", "ref": "L1", "text": "Order the kit"}
    assert ideas[2].cites == [SHED, "wiki/people/jane"]  # normalised
    assert ideas[3].counter == "They need a base too." and ideas[3].serves["type"] == "none"
    assert ideas[0].why_now == "The autumn sale ends Friday." and ideas[0].next_step
    assert ideas[0].counter == ""


def test_structure_problems_drop_the_candidate() -> None:
    ideas, dropped = generate.validate(
        answer(
            cand("reminder"),  # unknown kind
            cand(text=""),
            cand(text="x" * (generate.TEXT_CHARS + 1)),  # over-long
            cand(cites=[]),
            cand(cites="wiki/projects/garden-shed"),  # not a list
            cand(cites=[f"wiki/p/{n}" for n in range(generate.MAX_CITES + 1)]),
            cand("connect", "Only one page.", cites=[SHED]),  # connect needs two
            cand("challenge", "No counterargument."),
            cand(serves={"type": "goal", "ref": "G9"}),  # no such goal
            cand(serves={"type": "loop", "ref": "G1"}),  # a goal id for a loop
            cand(serves={"type": "project", "ref": "G1"}),
            cand(serves={"type": "none", "ref": "G1"}),
            cand(serves=None),
            cand(why_now=""),
            cand(why_now="Two\nlines."),
            cand(next_step="x" * (generate.LINE_CHARS + 1)),
            "just a string",
            cand(text="Kept.   With   spaces."),
        ),
        CTX,
        12,
    )
    assert [i.text for i in ideas] == ["Kept. With spaces."]
    assert len(dropped) == 17 and all(d.startswith("candidate ") for d in dropped)


def test_at_most_the_candidates_asked_for() -> None:
    many = [cand(text=f"Idea {n}.") for n in range(5)]
    ideas, dropped = generate.validate(answer(*many), CTX, 3)
    assert len(ideas) == 3 and len(dropped) == 2


# --- gate ---------------------------------------------------------------------------


def test_gate_drops_unknown_citations_and_ideas_resting_only_on_inferences() -> None:
    ok = Idea("follow-up", "Order the kit.", [DECISION, CHAT])
    made_up = Idea("continue", "Ask Bob.", ["wiki/people/bob"])
    mixed = Idea("connect", "Shed and Bob.", [SHED, "wiki/people/bob"])
    inferred_only = Idea("challenge", "Kits again.", [INFERRED], "No.")
    with_fact = Idea("challenge", "Kits, and the shed.", [INFERRED, SHED], "No.")
    kept, dropped = generate.gate(
        [ok, made_up, mixed, inferred_only, with_fact], {DECISION, CHAT, SHED, INFERRED}
    )
    assert kept == [ok, with_fact]
    assert len(dropped) == 3 and "only inferences" in dropped[2]


# --- prompts -------------------------------------------------------------------------


def _material() -> collect.Material:
    return collect.Material(
        pages=[
            collect.Item(SHED, "project", "Garden shed", "A shed."),
            collect.Item(INFERRED, "inference", "Prefers kits", "Maybe.", True, True),
        ],
        sources=[collect.Item(CHAT, "source", "Planning", "[m1 · User · x]\nI want a shed")],
    )


def test_prompts_carry_the_rules_material_context_and_feedback() -> None:
    system = generate.system_prompt("SHARED PROMPT", 12)
    assert system.startswith("SHARED PROMPT") and "# This task: ideas" in system
    assert "Propose up to 12 candidates" in system and "CANDIDATES" not in system

    ctx = Context(
        goals=CTX.goals,
        loops=CTX.loops,
        briefing="# Hot memory\n- builds",
        dream="## Direct changes\n- x",
    )
    useful = [Idea("connect", "Jane knows Acme.", [SHED], state="useful")]
    rejected = [
        Idea("challenge", "Kits are bad.", [SHED], state="not_useful"),
        Idea("follow-up", "Buy wood.", [SHED], state="already_doing"),
    ]
    seen = [Idea("follow-up", "Order the kit.", [SHED])]
    user = generate.user_prompt(
        _material(), ctx, Taste("Likes concrete steps."), useful, rejected, seen, "wiki"
    )
    assert "most recently updated pages" in user
    assert f'<doc id="{SHED}" type="project">' in user and "I want a shed" in user
    assert f'<doc id="{INFERRED}" type="inference" inferred="true" speculative="true">' in user
    assert (
        "G1: - Build a shed this autumn" in user
        and f"L1: [{SHED}] - [ ] Order the kit (2026-09)" in user
    )
    assert "<hot-memory>\n# Hot memory" in user and "<last-dream>\n## Direct changes" in user
    assert "<taste>\nLikes concrete steps." in user
    assert "<useful>\n- [connect] Jane knows Acme." in user
    assert "- [challenge] Kits are bad. (not useful)" in user
    assert "- [follow-up] Buy wood. (already doing)" in user
    assert "<already-seen>\n- [follow-up] Order the kit." in user
    assert "no hot-memory briefing" not in user
    assert user.index("<material>") < user.index("<goals>") < user.index("</material>")


def test_the_profile_stands_in_for_missing_hot_memory() -> None:
    ctx = Context(goals=[], briefing="# Owner profile\nLikes tools.", briefing_is_profile=True)
    user = generate.user_prompt(_material(), ctx, Taste(), [], [], [])
    assert "There is no hot-memory briefing yet" in user
    assert "<owner-profile>\n# Owner profile\nLikes tools." in user and "<hot-memory>" not in user
    assert "filed recently" in user and "G1" not in user and "(none set)" in user


async def test_ask_json_repairs_once() -> None:
    replies = iter(["not json", answer(cand())])
    seen: list[list[dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content)["messages"])
        return httpx2.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    llm = LLM("http://x", "k", transport=httpx2.MockTransport(handler))
    ideas, _ = await generate.generate(llm, "m", "sys", "user", CTX, 12)
    assert [i.text for i in ideas] == ["Order the kit."]
    assert "invalid" in seen[1][-1]["content"]
    await llm.close()


# --- scoring and the filters ----------------------------------------------------------


def test_score_parse() -> None:
    cands = [Idea("follow-up", f"Idea {n}.", [DECISION]) for n in range(4)]
    past = [Idea("follow-up", "Old.", [DECISION], id=7)]
    reply = {
        "scores": [
            {"id": "C1", "relevance": 4, "novelty": 5, "actionability": 3, "why_now": 4},
            {"id": "C2", "relevance": 6, "novelty": 5, "actionability": 3, "why_now": 4},  # 6
            {"id": "C3", "relevance": 3, "novelty": 1, "actionability": 3, "why_now": 3,
             "duplicate_of": "#7"},
            {"id": "C1", "relevance": 1, "novelty": 1, "actionability": 1, "why_now": 1},  # again
            {"id": "C9", "relevance": 5, "novelty": 5, "actionability": 5, "why_now": 5},  # unknown
            {"id": "C4", "relevance": True, "novelty": 5, "actionability": 5, "why_now": 5},
        ]
    }  # fmt: skip
    kept, dropped = score.parse(json.dumps(reply), cands, past)
    assert [i.text for i in kept] == ["Idea 0.", "Idea 2."]
    assert kept[0].score == {
        "relevance": 4,
        "novelty": 5,
        "actionability": 3,
        "why_now": 4,
        "total": 16,
    }
    assert kept[0].duplicate_of is None and kept[1].duplicate_of == 7
    assert len(dropped) == 2 and all("no valid score" in d for d in dropped)
    with pytest.raises(LLMError):
        score.parse('{"ideas": []}', cands, past)


def test_score_prompt_lists_candidates_and_past_ideas() -> None:
    c = scored()
    c.why_now, c.next_step = "Sale ends.", "Order."
    past = [
        Idea(
            "connect", "Old idea.", [SHED], id=3, state="dismissed", created="2026-08-01T00:00:00Z"
        )
    ]
    user = score.user_prompt(CTX, Taste(), [c], past)
    assert "C1: [follow-up] Order the kit." in user and "why now: Sale ends." in user
    assert "#3 (2026-08-01, dismissed): [connect] Old idea." in user
    assert "(no note yet)" in user and "G2: - Learn woodwork" in user


def test_threshold() -> None:
    good = scored(text="Good.")  # 16
    a_two = scored(text="A two.", scores=(5, 5, 5, 2))
    thirteen = scored(text="Thirteen.", scores=(4, 3, 3, 3))
    fourteen = scored(text="Fourteen.", scores=(5, 3, 3, 3))
    no_goal = scored(text="No goal.", serves="none")
    challenge = scored("challenge", "A challenge.", serves="none")
    kept, dropped = score.threshold([good, a_two, thirteen, fourteen, no_goal, challenge])
    assert kept == [good, fourteen, challenge]
    assert "under 3 on why_now" in dropped[0] and "total of 13" in dropped[1]
    assert "serves no goal or loop" in dropped[2]


def test_near_duplicates_of_the_last_180_days() -> None:
    past = [
        Idea("follow-up", "Order the garden shed kit before the sale ends", [DECISION, SHED], id=1),
    ]
    same = scored(text="Order the garden shed kit before the sale ends!", cites=[DECISION, CHAT])
    other_pages = scored(
        text="Order the garden shed kit before the sale ends", cites=[CHAT, INFERRED]
    )
    new = scored(text="Ask Jane about the concrete base.", cites=[DECISION])
    named = scored(text="Something else entirely.", cites=[CHAT])
    named.duplicate_of = 1
    kept, dropped = score.repeats([same, other_pages, new, named], past)
    assert kept == [other_pages, new]  # the same words resting on other pages is new
    assert "like idea #1" in dropped[0] and "repeats idea #1" in dropped[1]
    assert score.jaccard("a b c d e", "a b c d f") == pytest.approx(4 / 6)


def test_top_n_by_total() -> None:
    ideas = [scored(text=f"{n}", scores=(4, 4, 4, s)) for n, s in enumerate((3, 5, 4, 5))]
    kept, dropped = score.top(ideas, 3)
    assert [i.text for i in kept] == ["1", "3", "2"] and len(dropped) == 1
    shown, _ = score.select(ideas, [], 2)
    assert [i.text for i in shown] == ["1", "3"]


def test_context_serves_and_plain_lines() -> None:
    assert CTX.serves("goal", "G2") == "Learn woodwork (set 2026-09-01)"
    assert CTX.serves("goal", "G3") is None and CTX.serves("goal", "L1") is None
    assert CTX.serves("loop", "L1") == "Order the kit" and CTX.serves("loop", "L0") is None
    assert collect.plain("- [x] Done it") == "Done it"


# --- collect helpers ----------------------------------------------------------------


def test_changed_pages() -> None:
    commits = [
        {"files": [{"status": "M", "path": f"{SHED}.md"}, {"status": "A", "path": f"{CHAT}.md"}]},
        {
            "files": [
                {"status": "D", "path": "wiki/people/gone.md"},
                {"status": "M", "path": "log.md"},
            ]
        },
        {
            "files": [
                {"status": "A", "path": f"{SHED}.md"},
                {"status": "M", "path": f"{DECISION}.md"},
            ]
        },
        {"files": [{"status": "M", "path": "wiki/references/skipped-conversations.md"}]},
    ]
    assert collect.changed_pages(commits) == [SHED, DECISION]


def test_cited_sources_and_source_text() -> None:
    doc = {
        "frontmatter": {
            "sources": [
                {"id": "shed", "resource": "/raw/chats/claude/shed.md"},
                {"id": "web", "resource": "https://example.com"},
                "junk",
            ]
        }
    }
    assert collect.cited_sources(doc) == [CHAT]
    transcript = "# Shed\n\n## User · 2026-01-10 09:00 UTC\n\nI want a shed.\n"
    assert collect.source_text(transcript).startswith("[m1 · User · 2026-01-10 09:00 UTC]")
    assert collect.source_text("x" * 20_000).count("characters left out") == 1


# --- store ----------------------------------------------------------------------------


def test_store_dedupes_and_tracks_state(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    run = store.start_run(None, "m")
    added = store.add([Idea("follow-up", "Order the kit!", [SHED])], run.id)
    again = store.add(
        [Idea("follow-up", "order the kit", [SHED]), Idea("continue", "Order the kit!", [SHED])],
        run.id,
    )
    assert len(added) == 1 and [i.kind for i in again] == ["continue"]  # same words, same kind
    assert fingerprint("follow-up", "Order the KIT.") == fingerprint("follow-up", "order the kit")

    first = added[0].id
    assert store.set_state(first, "dismissed")
    assert not store.set_state(9999, "done")
    with pytest.raises(ValueError):
        store.set_state(first, "deleted")
    assert [i.kind for i in store.ideas()] == ["continue"]
    assert [i.id for i in store.feedback()] == [first]
    assert store.get(first) is not None and store.get(first).cites == [SHED]  # type: ignore[union-attr]
    assert store.counts()["dismissed"] == 1 and store.counts()["new"] == 1


def test_store_keeps_the_new_fields_and_orders_by_score(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    low, high = scored(text="Low.", scores=(3, 3, 4, 4)), scored(text="High.", scores=(5, 5, 5, 5))
    high.why_now, high.next_step = "Now.", "Do it."
    store.add([low, high])
    [first, second] = store.ideas(by="score")
    assert first.text == "High." and first.total == 20 and first.why_now == "Now."
    assert first.serves["type"] == "goal" and second.text == "Low."


def test_a_snoozed_idea_comes_back_once_its_date_has_passed(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    [idea] = store.add([Idea("continue", "Which base?", [SHED])])
    with pytest.raises(ValueError):
        store.set_state(idea.id, "snoozed", days=3)
    assert store.set_state(idea.id, "snoozed", days=7)
    assert store.ideas() == [] and store.get(idea.id).snoozed_until  # type: ignore[union-attr]
    assert [i.id for i in store.seen()] == [idea.id]  # still "already seen"
    store.db.execute("UPDATE ideas SET snoozed_until = '2020-01-01T00:00:00Z'")
    [back] = store.ideas()
    assert back.id == idea.id and back.state == "new" and back.snoozed_until is None


def test_already_doing_is_feedback(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    [idea] = store.add([Idea("follow-up", "Buy wood.", [SHED])])
    store.set_state(idea.id, "already_doing")
    assert [i.state for i in store.feedback()] == ["already_doing"] and store.ideas() == []


def test_store_from_before_the_scored_ideas_is_rebuilt(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "assistant.sqlite")
    db.executescript(
        "CREATE TABLE idea_runs (id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT, "
        "since TEXT, model TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0, proposed INTEGER NOT "
        "NULL DEFAULT 0, kept INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');"
        "INSERT INTO idea_runs (started, finished, model) VALUES ('2026-01-01', '2026-01-01', 'm');"
        "CREATE TABLE ideas (id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN "
        "('follow-up', 'continue', 'connect', 'challenge')), text TEXT NOT NULL, counter TEXT "
        "NOT NULL DEFAULT '', cites TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, state TEXT "
        "NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'useful', 'not_useful', 'dismissed', "
        "'done')), run_id INTEGER REFERENCES idea_runs(id), created TEXT NOT NULL, updated TEXT "
        "NOT NULL);"
        "CREATE INDEX ideas_state ON ideas(state, updated);"
        "INSERT INTO ideas VALUES (41, 'continue', 'Which base?', '', '[\"wiki/x\"]', 'fp', "
        "'useful', 1, '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');"
    )
    db.commit()
    db.close()
    store = IdeaStore(tmp_path)
    last = store.last_success()
    assert last is not None and last.scope == "recent"
    idea = store.get(41)
    assert idea is not None and idea.state == "useful" and idea.cites == ["wiki/x"]
    assert idea.serves == {} and idea.score == {}
    assert store.set_state(41, "already_doing")
    IdeaStore(tmp_path)  # opening it again changes nothing
    assert store.get(41).state == "already_doing"  # type: ignore[union-attr]


def test_the_taste_counter(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    assert store.taste() == Taste("", None, 0)
    assert [store.react() for _ in range(3)] == [1, 2, 3]
    saved = store.set_taste("Likes steps.")
    assert saved.reactions == 0 and store.taste().note == "Likes steps." and store.taste().updated


def test_store_last_success_skips_failed_runs(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    assert store.last_success() is None
    good = store.start_run("2026-01-01", "m")
    store.finish_run(good)
    bad = store.start_run(good.started, "m")
    bad.error = "LiteLLM unreachable"
    store.finish_run(bad)
    unfinished = store.start_run(good.started, "m")
    last = store.last_success()
    assert last is not None and last.id == good.id
    ended = store.last_finished()
    assert ended is not None and ended.id == bad.id
    assert store.last_run() is not None and store.last_run().id == unfinished.id  # type: ignore[union-attr]


def test_whole_wiki_runs_do_not_move_the_nightly_start(tmp_path: Path) -> None:
    store = IdeaStore(tmp_path)
    nightly = store.start_run("2026-01-01", "m", "recent")
    store.finish_run(nightly)
    ideate = store.start_run(None, "m", "wiki")
    store.finish_run(ideate)
    last = store.last_success()
    assert last is not None and last.id == nightly.id
    assert store.last_run() is not None and store.last_run().scope == "wiki"  # type: ignore[union-attr]


# --- the taste note ---------------------------------------------------------------------


def test_taste_parse_and_prompt() -> None:
    assert taste.parse('{"note": "  Likes steps. "}') == "Likes steps."
    for bad in ('{"note": ""}', '{"note": 3}', "[]", json.dumps({"note": "x" * 801})):
        with pytest.raises(LLMError):
            taste.parse(bad)
    user = taste.user_prompt(Taste("Old."), [Idea("challenge", "Kits.", [SHED], state="snoozed")])
    assert "<note>\nOld.\n</note>" in user and "- [challenge] Kits. -> snoozed" in user


def _taste_job(tmp_path: Path, replies: list[str]) -> tuple[IdeasJob, list[str]]:
    state, models = tmp_path / "state", priced_models(tmp_path / "models")
    systems: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        systems.append(json.loads(request.content)["messages"][0]["content"])
        content = replies.pop(0) if replies else '{"note": "Likes concrete next steps."}'
        usage = {"prompt_tokens": 100, "completion_tokens": 20}
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": content}}], "usage": usage}
        )

    job = IdeasJob(
        IdeaStore(state),
        WikiClient("http://127.0.0.1:9", "t" * 40),
        lambda: LLM("http://x", "k", transport=httpx2.MockTransport(handler)),
        _find("config"),
        make_budget(state, models),
    )
    return job, systems


async def test_the_taste_note_is_rewritten_after_the_tenth_reaction(tmp_path: Path) -> None:
    job, systems = _taste_job(tmp_path, [])
    [idea] = job.store.add([Idea("follow-up", "Buy wood.", [SHED])])
    job.store.set_state(idea.id, "useful")
    for _ in range(TASTE_EVERY - 1):
        assert not job.reacted()
    assert job.taste_task is None and systems == []
    assert job.reacted()  # the tenth
    assert job.taste_task is not None
    assert await job.taste_task == "Likes concrete next steps."
    note = job.store.taste()
    assert note.note == "Likes concrete next steps." and note.reactions == 0
    assert "You keep a short note" in systems[0]
    [call] = job.budget.calls("taste")
    assert call.pass_ == "taste" and call.model == "mistral-large"  # the cheap tier


async def test_the_taste_note_waits_for_a_run_and_keeps_a_bad_answer_out(tmp_path: Path) -> None:
    job, systems = _taste_job(tmp_path, ["not json", "still not json"])
    job.store.set_taste("Old note.")
    for _ in range(TASTE_EVERY - 1):
        job.reacted()
    async with job._lock:  # a dream or ideas run is going
        assert not job.reacted()
    assert systems == [] and job.store.taste().reactions == TASTE_EVERY
    assert job.reacted()  # the next reaction tries again
    assert job.taste_task is not None and await job.taste_task is None  # both answers bad
    assert job.store.taste().note == "Old note." and len(systems) == 2
    assert job.store.taste().reactions == TASTE_EVERY + 1  # counted on: the next one retries
    job.budget.automation_eur = 0.0  # refused: skipped, no call
    assert job.reacted()
    assert job.taste_task is not None and await job.taste_task is None
    assert len(systems) == 2


# --- schedule ---------------------------------------------------------------------------


def test_next_run() -> None:
    at = parse_time("03:30")
    assert next_run(at, dt.datetime(2026, 9, 28, 1, 0)) == dt.datetime(2026, 9, 28, 3, 30)
    assert next_run(at, dt.datetime(2026, 9, 28, 3, 30)) == dt.datetime(2026, 9, 29, 3, 30)
    assert next_run(at, dt.datetime(2026, 12, 31, 23, 0)) == dt.datetime(2027, 1, 1, 3, 30)


# --- progress ---------------------------------------------------------------------------


def test_progress_text_and_status() -> None:
    from portal.ideas.progress import STEPS, Progress

    p = Progress("wiki", "glm-5-3")
    assert p.text == "Finding pages"
    p.expect(3)
    p.read()
    assert p.text == "Reading page 1 of 3" and (p.done, p.total) == (1, 3)
    p.read(2)
    p.to(2)
    assert "Waiting for glm-5-3" in p.text
    p.to(4)
    assert p.text == "Scoring the suggestions"
    p.to(5)
    p.kept = 1
    p.end()
    status = p.as_dict()
    assert status["finished"] and status["steps"] == list(STEPS) and len(STEPS) == 5
    assert status["text"] == "Done: 1 new idea from 3 pages and conversations."
    failed = Progress("recent", "m")
    failed.end("LiteLLM unreachable")
    assert failed.text == "Stopped: LiteLLM unreachable" and failed.as_dict()["error"]
    empty = Progress("recent", "m")
    empty.end()
    assert empty.text == "Nothing new to read."
    repeated = Progress("wiki", "m")
    repeated.read(4)
    repeated.proposed = 2
    repeated.end()
    assert repeated.text.startswith("Done: nothing new from 4 pages and conversations.")
    assert "scored too low" in repeated.text
    quiet = Progress("wiki", "m")
    quiet.read(4)
    quiet.end()
    assert (
        quiet.text == "Done: the model found nothing worth suggesting in 4 pages and conversations."
    )


async def test_stop_cancels_a_taste_rewrite(tmp_path: Path) -> None:
    job, _ = _taste_job(tmp_path, [])
    job.taste_task = asyncio.create_task(asyncio.sleep(10))  # type: ignore[assignment]
    await job.stop()
    assert job.taste_task.cancelled()

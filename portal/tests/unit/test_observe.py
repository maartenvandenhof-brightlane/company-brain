"""Observing a chat (processing/observe.py): the answer's contract, merging chunks, and the
message numbers wiki-mcp checks the observations against."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from wiki_mcp.wiki import observations

from portal.processing import condense
from portal.processing.llm import LLM, LLMError
from portal.processing.observe import EXTRACTOR, Observed, merge, observe, parse, payload

OBS = {
    "kind": "action",
    "basis": "reported",
    "text": "The owner postponed the release.",
    "msgs": [3, "4", True],
    "quote": "I pushed the release back",
    "event": {"summary": "Release postponed", "when": "2026-03", "key": "release"},
    "span": "sometimes",
    "context": "work",
    "valence": "difficulty",
    "endorsed": "yes",
}


def test_parse_keeps_the_contract_and_drops_the_rest() -> None:
    answer = {
        "classification": {
            "types": ["work", "gossip"],
            "contexts": ["work"],
            "intents": ["decide", 7],
            "emotions": [
                {"emotion": "Frustrated", "target": "tests", "kind": "stated", "msg": 3,
                 "quote": "so annoying"},
                {"emotion": "sad", "kind": "guess", "msg": 3, "quote": "x"},
            ],
            "sentiment": "grim",
        },
        "observations": [
            OBS,
            {**OBS, "kind": "personality"},
            {**OBS, "msgs": []},
            {**OBS, "quote": ""},
            "not an object",
        ],
    }  # fmt: skip
    out = parse(json.dumps(answer))
    c = out.classification
    assert c["types"] == ["work"] and c["intents"] == ["decide"] and "sentiment" not in c
    assert [e["emotion"] for e in c["emotions"]] == ["frustrated"]
    [kept] = out.observations
    assert kept["msgs"] == [3] and kept["span"] == "unknown" and kept["endorsed"] is False
    with pytest.raises(LLMError):
        parse("[]")
    with pytest.raises(LLMError):
        parse("no json")


def test_merging_chunks_renumbers_and_keeps_events_apart() -> None:
    one = Observed({"types": ["work"], "contexts": ["work"], "intents": [], "emotions": []},
                   [dict(OBS), dict(OBS)])  # fmt: skip
    two = Observed(
        {"types": ["reflection"], "contexts": ["work"], "sentiment": "mixed"}, [dict(OBS)]
    )
    merged = merge([one, two])
    assert [o["id"] for o in merged.observations] == ["o1", "o2", "o3"]
    assert [o["event"]["key"] for o in merged.observations] == ["c1-release"] * 2 + ["c2-release"]
    assert merged.classification["types"] == ["work", "reflection"]
    assert merged.classification["sentiment"] == "mixed"
    single = merge([one])
    assert [o["id"] for o in single.observations] == ["o1", "o2"]
    assert payload("raw/chats/claude/x", single)["extractor"] == EXTRACTOR


def _body(*messages: tuple[str, str], start: int = 0) -> str:
    head = "# Chat\n\n- Source: Claude.ai conversation\n- Started: 2026-01-10 09:00 UTC\n"
    if start:
        head += f"- Continuation: messages {start} to 9; the first {start - 1} are elsewhere\n"
    return head + "\n" + "\n".join(f"## {r} · 2026-01-10 09:0{i} UTC\n\n{t}\n"
                                   for i, (r, t) in enumerate(messages))  # fmt: skip


@pytest.mark.parametrize(
    "bodies",
    [
        [_body(("User", "a"), ("Assistant", "b")), _body(("User", "c"), ("Assistant", "d"))],
        [_body(("User", "a"), ("Assistant", "b"), start=5), _body(("User", "c"))],
    ],
)
def test_the_portal_and_wiki_mcp_number_messages_alike(bodies: list[str]) -> None:
    """wiki-mcp checks the msgs the model cites against its own parse of the transcript."""
    transcript = condense.Transcript()
    for body in bodies:
        condense.parse(body, transcript)
    mine = [(m.number, m.role, m.text) for m in transcript.messages]
    theirs = [(m.number, m.role, m.text) for m in observations.parse(bodies)]
    assert mine == theirs


async def test_observe_repairs_a_broken_answer_once() -> None:
    answers = iter(["not json", json.dumps({"observations": [OBS]})])
    seen: list[list[dict[str, Any]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content)["messages"])
        return httpx2.Response(200, json={"choices": [{"message": {"content": next(answers)}}]})

    llm = LLM("http://x", "k", transport=httpx2.MockTransport(handler))
    out = await observe(llm, "m", ["[m3 · User · 2026-01-10]\nI pushed the release back"], "")
    assert [o["id"] for o in out.observations] == ["o1"]
    assert len(seen) == 2 and "That answer is invalid" in seen[1][-1]["content"]
    assert "Only the owner's (User) messages are evidence" in seen[0][0]["content"]
    await llm.close()

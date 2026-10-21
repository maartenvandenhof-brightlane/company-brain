"""Batch processing end to end: a real wiki-mcp, a fake LiteLLM."""

from __future__ import annotations

import json
from typing import Any

import httpx2

from portal.processing.llm import LLM
from portal.processing.runner import ProcessJob, Processor
from portal.wikiclient import WikiClient


def chat(key: str, title: str, *messages: tuple[str, str]) -> dict[str, Any]:
    body = f"# {title}\n\n- Source: Claude.ai conversation\n- Started: 2026-01-10 09:00 UTC\n\n"
    body += "\n".join(
        f"## {r} · 2026-01-10 09:0{i} UTC\n\n{t}\n" for i, (r, t) in enumerate(messages)
    )
    return {"key": key, "title": title, "content": body, "messages": len(messages)}


SHED = chat(
    "claude-chat:shed",
    "Planning the garden shed",
    ("User", "I want a shed; Jane Doe from Acme will help."),
    ("Assistant", "Kit or own design?"),
    ("User", "Kit."),
)
HELLO = chat("claude-chat:hello", "Hello", ("User", "hi"), ("Assistant", "Hello!"))
BROKEN = chat("claude-chat:broken", "Broken", ("User", "something"), ("Assistant", "ok"))

SHED_ANSWER = {
    "skip": None,
    "entities": [
        {
            "key": "s",
            "ref": None,
            "type": "project",
            "title": "Garden shed",
            "description": "A shed.",
        },
        {"key": "j", "ref": None, "type": "person", "title": "Jane Doe", "description": "Helps."},
        {
            "key": "a",
            "ref": None,
            "type": "organization",
            "title": "Acme",
            "description": "A firm.",
        },
        {
            "key": "me",
            "ref": "wiki/owner/profile",
            "type": "owner",
            "title": "Owner profile",
            "description": "",
        },
    ],
    "facts": [
        {
            "entity": "s",
            "section": "Timeline",
            "text": "Jane Doe will help build it.",
            "as_of": "2026-01",
            "basis": "user",
        },
        {
            "entity": "me",
            "section": "Preferences",
            "text": "Prefers kits.",
            "as_of": "2026-01",
            "basis": "accepted",
        },
        {
            "entity": "j",
            "section": "Facts",
            "text": "Is a famous architect.",
            "as_of": "2026-01",
            "basis": "assistant",
        },
    ],
    "decisions": [
        {
            "entity": "s",
            "title": "Build the shed from a kit",
            "context": "Choosing how to build.",
            "options": ["kit", "own design"],
            "outcome": "A kit.",
            "rationale": "Simpler.",
            "as_of": "2026-01",
            "basis": "user",
        }
    ],
    "relations": [{"from": "j", "to": "a", "relation": "works at"}],
}


def fake_litellm() -> tuple[httpx2.MockTransport, list[str]]:
    prompts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        prompts.append(prompt)
        if "I want a shed" in prompt:
            answer = json.dumps(SHED_ANSWER)
        elif "Conversation: Hello" in prompt:
            answer = json.dumps({"skip": "small talk"})
        else:
            answer = "this is not JSON"
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": answer}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
            },
        )

    return httpx2.MockTransport(handler), prompts


async def test_process_imported_chats(wiki: WikiClient) -> None:
    await wiki.import_chats([SHED, HELLO, BROKEN])
    transport, prompts = fake_litellm()
    llm = LLM("http://litellm:4000", "k", transport=transport)
    job = ProcessJob(model="glm-5-3")
    await Processor(wiki, llm, concurrency=2).run(job)

    assert job.finished and not job.error
    assert (job.total, job.done, job.skipped, job.failed) == (3, 1, 1, 1)
    assert job.dropped == 1 and job.created >= 5 and job.updated == 0
    assert job.calls == 4 and job.input_tokens == 4000  # broken: one call and one repair
    shed_prompt = next(p for p in prompts if "I want a shed" in p)
    assert "Kit or own design?" in shed_prompt and "Existing wiki pages" in shed_prompt

    shed = await wiki.doc("wiki/projects/garden-shed")
    fm = shed["frontmatter"]
    assert fm["status"] == "draft" and "from-chat-import" in fm["tags"]
    assert fm["sources"] == [{"id": "shed", "resource": "/raw/chats/claude/shed.md"}]
    assert "[Jane Doe](/wiki/people/jane-doe.md) will help build it. [^shed]" in shed["body"]
    assert (
        "[Build the shed from a kit](/wiki/decisions/build-the-shed-from-a-kit.md)" in shed["body"]
    )
    jane = await wiki.doc("wiki/people/jane-doe")
    assert "famous architect" not in jane["body"]  # assistant-only
    assert jane["frontmatter"]["related"] == ["wiki/organizations/acme"]
    acme = await wiki.doc("wiki/organizations/acme")
    assert "wiki/people/jane-doe" in [b["id"] for b in acme["backlinks"]]
    owner = await wiki.doc("wiki/owner/profile")
    assert "- 2026-01: Prefers kits. [^shed]" in owner["body"]
    skipped = await wiki.doc("wiki/references/skipped-conversations")
    assert "[Hello](/raw/chats/claude/hello.md): small talk" in skipped["body"]

    left = await wiki.unprocessed()
    assert [s["id"] for s in left["sources"]] == ["raw/chats/claude/broken"]  # offered again

    again = ProcessJob(model="glm-5-3")
    await Processor(wiki, LLM("http://x", "k", transport=transport), concurrency=1).run(again)
    assert again.total == 1 and again.failed == 1


async def test_nothing_to_process(wiki: WikiClient) -> None:
    transport, prompts = fake_litellm()
    job = ProcessJob(model="m", scope="older")
    await Processor(wiki, LLM("http://x", "k", transport=transport)).run(job)
    assert job.finished and job.total == 0 and prompts == []


async def test_process_everything_again_replaces_unreviewed_lines(wiki: WikiClient) -> None:
    await wiki.import_chats([SHED, HELLO])
    transport, _ = fake_litellm()
    await Processor(wiki, LLM("http://x", "k", transport=transport)).run(ProcessJob(model="m"))
    assert (await wiki.unprocessed())["total"] == 0
    everything = await wiki.unprocessed(scope="all")
    assert everything["total"] == 2 and all(s.get("filed_at") for s in everything["sources"])

    # The owner reviews Jane's page (no tag, stable); the shed page is still a draft.
    # A second run words the shed fact differently, and HELLO now has something to file.
    SHED_ANSWER["facts"][0]["text"] = "Jane Doe offered to help with the build."
    hello_answer = {
        "entities": [
            {
                "key": "me",
                "ref": "wiki/owner/profile",
                "type": "owner",
                "title": "Owner profile",
                "description": "",
            }
        ],
        "facts": [
            {
                "entity": "me",
                "section": "Background",
                "text": "Says hi a lot.",
                "as_of": "2026-01",
                "basis": "user",
            }
        ],
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        answer = SHED_ANSWER if "I want a shed" in prompt else hello_answer
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": json.dumps(answer)}}], "usage": {}}
        )

    try:
        job = ProcessJob(model="m", scope="all")
        await Processor(wiki, LLM("http://x", "k", transport=httpx2.MockTransport(handler))).run(
            job
        )
    finally:
        SHED_ANSWER["facts"][0]["text"] = "Jane Doe will help build it."
    assert job.total == 2 and job.done == 2 and not job.failed

    shed = (await wiki.doc("wiki/projects/garden-shed"))["body"]
    assert "offered to help with the build" in shed and "will help build it" not in shed
    assert shed.count("[^shed]: [Planning the garden shed]") == 1  # the footnote, once
    skipped = (await wiki.doc("wiki/references/skipped-conversations"))["body"]
    assert "raw/chats/claude/hello.md" not in skipped  # filed now: no longer skipped
    owner = (await wiki.doc("wiki/owner/profile"))["body"]
    assert "Says hi a lot. [^hello]" in owner and "Prefers kits. [^shed]" in owner


FRIEND = chat(
    "claude-chat:friend",
    "Falling out with Sam",
    ("User", "My best friend cancelled our trip and I feel let down. The shed is done, though."),
    ("Assistant", "That sounds hard."),
)
LONELY = chat("claude-chat:lonely", "Lonely evening", ("User", "just sad"), ("Assistant", "Hugs."))

FRIEND_ANSWER = {
    "skip": None,
    "private_conversation": True,
    "entities": [
        {"key": "b", "type": "person", "title": "Best friend (unnamed)", "private": True},
        {"key": "s", "type": "project", "title": "Garden shed", "description": "A shed."},
        {"key": "me", "ref": "wiki/owner/profile", "type": "owner", "title": "Owner profile"},
    ],
    "facts": [
        {"entity": "b", "section": "Feelings", "text": "Cancelled the trip.", "basis": "user"},
        {
            "entity": "me",
            "section": "Feelings",
            "text": "Feels let down.",
            "basis": "user",
            "private": True,
        },
        {"entity": "s", "section": "Timeline", "text": "Finished.", "basis": "user"},
    ],
    "relations": [{"from": "b", "to": "s", "relation": "helped with"}],
}


async def test_process_a_private_conversation(wiki: WikiClient) -> None:
    await wiki.import_chats([FRIEND, LONELY])

    def handler(request: httpx2.Request) -> httpx2.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        answer = (
            FRIEND_ANSWER
            if "cancelled our trip" in prompt
            else {"skip": "only sadness", "private_conversation": True}
        )
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": json.dumps(answer)}}], "usage": {}}
        )

    job = ProcessJob(model="m")
    await Processor(wiki, LLM("http://x", "k", transport=httpx2.MockTransport(handler))).run(job)
    assert job.done == 1 and job.skipped == 1 and not job.failed and job.private == 2

    friend = await wiki.doc("wiki/people/best-friend-unnamed")
    assert friend["private"] is True and friend["frontmatter"]["private"] is True
    assert friend["frontmatter"]["related"] == ["wiki/projects/garden-shed"]
    personal = await wiki.doc("wiki/owner/personal")
    assert personal["private"] is True and personal["frontmatter"]["type"] == "owner"
    assert "Feels let down. [^friend]" in personal["body"]
    shed = await wiki.doc("wiki/projects/garden-shed")
    assert not shed["private"] and "private" not in shed["frontmatter"]
    assert "best-friend" not in shed["body"] and "related" not in shed["frontmatter"]
    assert "Falling out" not in shed["body"]  # the private conversation's title
    assert "[^friend]: [Private conversation](/raw/chats/claude/friend.md)" in shed["body"]
    for sid in ("raw/chats/claude/friend", "raw/chats/claude/lonely"):
        source = await wiki.doc(sid)
        assert source["private"] is True, sid
    skipped = (await wiki.doc("wiki/references/skipped-conversations"))["body"]
    assert "- [Private conversation](/raw/chats/claude/lonely.md)" in skipped
    assert "Lonely" not in skipped and "only sadness" not in skipped
    assert (await wiki.unprocessed())["total"] == 0

"""Observations of one imported chat: what the owner's own messages show about him, the
evidence for personal patterns (wiki-mcp's wiki/observations.py has the page and its checks).

The contract is wiki-mcp's (wiki_mcp.dream.observe: SYSTEM, the user prompt, the clamp and the
merge), shared with the observe tools that let Claude.ai or ChatGPT observe chats with their
own model. This is the portal's side: one JSON call per condensed transcript chunk
(condense.py), and one repair call when the answer breaks the contract; the chunks of a long
transcript are merged (observation ids renumbered, event keys kept apart per chunk). wiki-mcp
checks every observation against the transcript again (only the owner's messages are
evidence, quotes are verbatim) and writes the page: POST /observations, one commit per chat,
idempotent per extractor version (EXTRACTOR).

Batch processing observes each chat it files when the run's `observe` option is on (one extra
call per chat, with the same model); the Patterns page's backfill (patterns/backfill.py)
observes the chats that have no observation page yet.
"""

from __future__ import annotations

from wiki_mcp.dream import Unparsable
from wiki_mcp.dream import observe as contract
from wiki_mcp.dream.observe import (
    EXTRACTOR,
    MAX_TOKENS,
    SYSTEM,
    Observed,
    merge,
    part_label,
    payload,
    user_prompt,
)

from portal.processing.llm import LLM, LLMError

__all__ = [
    "EXTRACTOR",
    "MAX_TOKENS",
    "SYSTEM",
    "Observed",
    "merge",
    "observe",
    "parse",
    "payload",
    "user_prompt",
]


def parse(text: str) -> Observed:
    """The model's answer clamped to the contract (wiki_mcp.dream.observe.parse); LLMError
    when it is no JSON object, so the call is repaired once."""
    try:
        return contract.parse(text)
    except Unparsable as exc:
        raise LLMError(str(exc)) from exc


async def observe(llm: LLM, model: str, chunks: list[str], started: str) -> Observed:
    """One call per chunk (and one repair call each when its answer breaks the contract)."""
    results: list[Observed] = []
    for i, chunk in enumerate(chunks, 1):
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt(started, chunk, part_label(i, len(chunks)))},
        ]
        answer = await llm.json_chat(model, messages, max_tokens=MAX_TOKENS)
        try:
            results.append(parse(answer))
        except LLMError as exc:
            messages += [
                {"role": "assistant", "content": answer[:20000]},
                {
                    "role": "user",
                    "content": f"That answer is invalid ({exc}). "
                    "Reply with only the corrected JSON object.",
                },
            ]
            results.append(parse(await llm.json_chat(model, messages, max_tokens=MAX_TOKENS)))
    return merge(results) if results else Observed()

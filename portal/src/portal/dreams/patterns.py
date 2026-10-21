"""The patterns pass: personal patterns from observations, as pattern pages. Direct, one commit.

The material, the prompt, the checks and the operations are wiki-mcp's (wiki_mcp.dream.patterns
has the whole pass described; wiki_mcp.wiki.patterns the rules wiki-mcp holds every page to):
wiki-mcp builds the material from every observation page (GET /patterns/material). This is the
portal's side: ask the strong model (the cheap one when the budget says so) once, log what the
checks dropped (by position and reason, never the hypothesis or a quote), and commit the pages.

It runs only in a patterns run (DreamJob.reassess, the Patterns page's "Reassess patterns"),
never in the scheduled dream: each run is one model call over up to 60,000 characters, which
the owner starts when he wants the patterns brought up to date.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wiki_mcp.dream.patterns import MAX_TOKENS, NAME, Material, parse, review, system_prompt

if TYPE_CHECKING:
    from portal.dreams.job import Context


async def patterns_pass(ctx: Context) -> None:
    """Ask, check and commit."""
    found = Material.of(await ctx.wiki.patterns_material())
    if not found.refs:
        ctx.log.skip(NAME, "no observations yet: observe chats on the Patterns page first")
        return
    answer = await ctx.ask(NAME, "strong", system_prompt(), found.text, MAX_TOKENS, parse)
    if answer is None:
        return
    if answer.notes:
        ctx.log.flag(NAME, f"model notes: {answer.notes}")
    plan = review(answer, found)
    for why in plan.dropped:
        ctx.log.flag(NAME, f"dropped: {why}")
    for flag in plan.flags:
        ctx.log.flag(NAME, flag)
    for skew in found.coverage.get("skew") or []:
        ctx.log.flag(NAME, f"coverage: {skew}")
    if not plan.ops:
        ctx.log.skip(NAME, f"nothing to write from {len(found.refs)} sampled observations")
        return
    await ctx.commit(NAME, plan.reason(found.observations), plan.ops, plan.lines)

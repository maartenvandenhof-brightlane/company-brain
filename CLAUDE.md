# Second Brain — notes for Claude Code

## Investigating the codebase: delegate to a Sonnet subagent

When a task needs codebase investigation — locating where something lives, tracing a flow,
understanding how modules connect, surveying code before a change — do not explore it
yourself in the main conversation. Spawn the `codebase-investigator` subagent
(`.claude/agents/codebase-investigator.md`, runs on Sonnet 5) with a specific question and the
level of thoroughness wanted, then act on its report.

- Give it a self-contained prompt: it does not see this conversation. Include the goal, the
  names/paths you already know, and what you need back.
- Independent questions can go to several investigators in parallel.
- Reading a file you already know you need to edit, or a quick lookup of a single known
  path, does not need a subagent; do that directly.
- The investigator is read-only. Edits, tests and commits stay with the main agent.

## Coding tasks: delegate to the Opus coder subagent

Hand implementation work — writing or changing code, bug fixes, refactors, tests — to the
`coder` subagent (`.claude/agents/coder.md`). It is pinned to Opus 5.5
(`model: claude-opus-5-5`) and must never run on Sonnet: do not override its model when
spawning it, and do not use a Sonnet agent (including `codebase-investigator`) for coding.

- Typical flow: `codebase-investigator` finds the relevant code, then `coder` gets a brief
  built from that report: goal, known files and line references, constraints, and how to
  verify.
- Its brief must be self-contained; it does not see this conversation.
- Tiny mechanical edits (a typo, a one-line config value) can be made directly.
- The main agent reviews the coder's report and diff, and owns commits and anything that
  touches the running stack.

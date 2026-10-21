---
type: howto
id: wiki/howtos/using-this-wiki
title: Using this wiki
description: How to add knowledge to the wiki and get answers from it through any connected assistant.
tags: [meta]
status: stable
related:
  - wiki/concepts/company-brain
---

# Using this wiki

## Asking questions

Ask any connected assistant (Claude, ChatGPT or Gemini with the brain connected) something like
*"What do we know about Acme's renewal?"*. It searches the wiki first and cites the
pages it used.

## Adding knowledge

- *"Remember that Jane Doe moved to Acme as CTO."* The assistant updates or creates
  `wiki/people/jane-doe` and links it to `wiki/organizations/acme`.
- *"File these meeting notes."* The assistant ingests the raw notes as a source, then creates a
  meeting page that cites it.

## Reviewing

- `log.md` and the git history show every change, who made it and why.
- `proposals/` holds changes derived from untrusted material. Ask an assistant to *review
  proposals* and then accept or reject them.
- A lint report is written nightly under `wiki/maintenance/`.

Background: [Company Brain](/wiki/concepts/company-brain.md).

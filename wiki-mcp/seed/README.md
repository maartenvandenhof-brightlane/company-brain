---
type: guide
id: README
title: About this wiki
description: What this knowledge bundle is, how it is organised and how to read it.
tags: [meta]
status: stable
---

# Company Brain wiki

This is the company's knowledge base in [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format)
(OKF v0.2): a folder of markdown files with YAML frontmatter, kept in git. AI assistants maintain it
through an MCP server, and you can read or edit it in any text editor or in Obsidian.

## Layout

| Path | What lives there |
| --- | --- |
| `index.md` | Generated table of contents, grouped by type |
| `log.md` | Human-readable change log, newest first (git has the full history) |
| `AGENTS.md` | Rules for AI agents working on this wiki |
| `context/` | Session context every conversation starts with: `soul`, `user`, `memory` |
| `wiki/<type>/` | Concept pages: people, organizations, projects, concepts, decisions, meetings, how-tos, references |
| `raw/` | Immutable source material that pages cite |
| `proposals/` | Changes awaiting owner review |

Start at the [index](/index.md), or read [how to use this wiki](/wiki/howtos/using-this-wiki.md).

## Editing by hand

You may edit files directly (for example in Obsidian or VS Code on the wiki folder). Keep the
frontmatter valid. Search picks up your changes within seconds; the server commits them to git at
its next start, and the nightly lint report flags problems.

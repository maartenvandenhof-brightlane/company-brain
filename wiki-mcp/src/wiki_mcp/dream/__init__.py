"""Dreams without the model call: what each pass reads, the prompts it sends, and the checks
its answer must pass before anything is written.

The portal's dreams (portal/dreams/) and the dream tools for assistants share this code: they
ask a model (the portal its own, an assistant itself) with the same prompts and material,
and turn its answer into the same commits and proposals with the same checks. wiki-mcp checks
every change once more when it is written (wiki/dreamops.py, wiki/facts.py).

  pages       the reviewed pages a dream reads, numbered for the material
  reader      the reads it needs from the wiki (WikiReader)
  prompts     the passes' prompts
  hygiene     D4: small fixes on reviewed pages, in code
  merge       D4: duplicate pages, as merge proposals
  deduction   D2: supersedes, conflicts, implications
  induction   D3: patterns across stated lines
  inference   inference pages as deduction and induction write them
  hot_memory  D5: the briefing
  patterns    personal patterns from observations (the portal's "Reassess patterns", and a
              patterns session in an assistant: reassess.py; no assistant dream runs it)
  observe     the observation extraction's contract (the portal's calls, observing.py)
  log         D6: the dream log page
  limits      caps on what one model call reads
"""

from __future__ import annotations


class Unparsable(ValueError):
    """A model's answer is not the JSON object the pass asked for; the message says why (the
    portal asks once more with it)."""

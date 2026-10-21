"""Deterministic secret redaction for chat text, before anything leaves the portal.

The patterns here are broader than wiki-mcp's own credential check. As a last step,
every line that check (wiki_mcp.wiki.secrets_scan.find_secrets) still flags is
replaced as a whole, so a redacted transcript can never be rejected by the store.
Only counts per kind are reported; the matched text is never logged or kept.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from wiki_mcp.wiki.secrets_scan import find_secrets

# Short on purpose: "[secret" stays under the 8 characters wiki-mcp's assignment
# pattern needs, so `password: [secret removed]` is not itself flagged.
MARK = "[secret removed]"
_ASSIGN_KEYS = (
    r"password|passwd|pwd|passphrase|secret|client[_-]?secret|api[_-]?key|apikey|"
    r"access[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|token"
)

# (kind, pattern, replacement). Order matters: whole blocks before their pieces.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.S,
        ),
        MARK,
    ),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), MARK),
    (
        "OpenAI-style API key",
        re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
        MARK,
    ),
    ("Stripe key", re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{16,}"), MARK),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MARK),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), MARK),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), MARK),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MARK),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), MARK),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), MARK),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MARK),
    ("bearer token", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), rf"\1 {MARK}"),
    (
        "credentials in a URL",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):[^\s@/]+@"),
        rf"\1:{MARK}@",
    ),
    (
        "password assignment",
        # Also DB_PASSWORD=… style names. The value's quotes go too: `"[secret` would be
        # long enough to trip wiki-mcp's own check.
        re.compile(
            rf"(?i)\b([A-Za-z0-9_-]*(?:{_ASSIGN_KEYS})[\"']?[ \t]*[:=][ \t]*)"
            r"[\"']?(?!\[secret removed\])[^\s\"',;]{6,}[\"']?"
        ),
        rf"\1{MARK}",
    ),
]


@dataclass
class Redaction:
    text: str
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact(text: str) -> Redaction:
    counts: Counter[str] = Counter()
    for kind, pattern, replacement in _RULES:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[kind] += n
    if find_secrets(text):  # whatever the store would still refuse: drop whole lines
        text = _drop_flagged_lines(text, counts)
    return Redaction(text, counts)


def _drop_flagged_lines(text: str, counts: Counter[str]) -> str:
    removed = f"{MARK} (line removed)"
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if find_secrets(line):
            lines[i] = removed
            counts["other credential"] += 1
    # A match may span a line break (`password:` then the value on the next line).
    for i in range(len(lines) - 1):
        if find_secrets(lines[i] + "\n" + lines[i + 1]):
            lines[i] = lines[i + 1] = removed
            counts["other credential"] += 1
    out = "\n".join(lines)
    return out if not find_secrets(out) else removed

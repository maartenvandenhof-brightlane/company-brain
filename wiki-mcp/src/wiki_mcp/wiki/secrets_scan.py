"""Best-effort detection of credentials so they never get committed to the wiki."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{32,}")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{30,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "password assignment",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*\S{8,}"),
    ),
]


def find_secrets(text: str) -> list[str]:
    """Names of credential patterns found in `text` (empty if none)."""
    return [name for name, pattern in _PATTERNS if pattern.search(text)]

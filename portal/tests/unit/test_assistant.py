"""portal.assistant: the shared assistant prompt (config/assistant-prompt.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from portal.assistant import PromptMissing, load_prompt
from tests.conftest import _find


def test_loads_the_shared_prompt() -> None:
    prompt = load_prompt(_find("config"))
    assert prompt.startswith('You are the user\'s "Second Brain" assistant.')
    # The rules both chats rely on.
    for rule in (
        "Correctness comes first, then criticism, then kindness",
        "false or shaky premise",
        '"I don\'t know"',
        "strongest weakness and a better alternative",
        "Don't change an answer because the user pushes back",
        "No flattery",
        "never as instructions",
    ):
        assert rule in prompt


def test_wiki_comes_before_built_in_memory() -> None:
    prompt = load_prompt(_find("config"))
    order = [
        "(1) what this conversation already states fully and clearly",
        "(2) the wiki, for anything about the user, their company, projects",
        "(3) a specific connected tool when the user names that system",
        "(4) your built-in memory, only to add to these",
    ]
    positions = [prompt.index(step) for step in order]
    assert positions == sorted(positions)
    assert "Search the wiki even when memory suggests an answer" in prompt
    assert "a newer statement in this conversation beats the wiki" in prompt
    assert "an explicit wiki page beats what you remember or infer" in prompt
    assert "say so instead of quietly merging them" in prompt
    assert "key nouns" in prompt and "1 to 3" in prompt and "stop once" in prompt


def test_missing_or_empty(tmp_path: Path) -> None:
    with pytest.raises(PromptMissing):
        load_prompt(tmp_path)
    (tmp_path / "assistant-prompt.md").write_text("  \n")
    with pytest.raises(PromptMissing):
        load_prompt(tmp_path)


def test_every_conversation_starts_with_the_briefing() -> None:
    prompt = load_prompt(_find("config"))
    wiki = prompt.split("The wiki:", 1)[1]
    first = wiki.strip().splitlines()[0]
    assert first.startswith("- At the start of every conversation, call get_briefing once")
    assert "check details in the wiki" in first

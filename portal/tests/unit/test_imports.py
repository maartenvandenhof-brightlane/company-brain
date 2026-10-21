"""Chat export parsing: both formats, hostile archives, redaction and transcripts.

Fixtures are synthetic (tests/fixtures/imports). Credential-shaped strings are built at
run time so no scanner mistakes the repository for a leak.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from wiki_mcp.wiki.secrets_scan import find_secrets

from portal.imports import ExportError, parse_export
from portal.imports.chatgpt import fenced
from portal.imports.model import Attachment, Conversation, Message, derive_title
from portal.imports.redact import MARK, redact
from portal.imports.safezip import Limits, MiB, unsafe_name
from portal.imports.transcript import MAX_PART_BYTES, render, tidy_markdown

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "imports"
CLAUDE = FIXTURES / "claude"
CHATGPT = FIXTURES / "chatgpt"

ANTHROPIC_KEY = "sk-ant-" + "api03-" + "Ab1" * 14
OPENAI_KEY = "sk-" + "proj-" + "Zy9" * 14
GITHUB_TOKEN = "ghp" + "_" + "Q" * 36
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "s" * 20
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA " + "PRIVATE KEY-----"

ZipMaker = Callable[..., Path]


@pytest.fixture
def make_zip(tmp_path: Path) -> ZipMaker:
    def make(members: dict[str, bytes | str], name: str = "export.zip") -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    return make


def _fixture(folder: Path, name: str) -> str:
    return (folder / name).read_text(encoding="utf-8")


def _claude_zip(make_zip: ZipMaker) -> Path:
    return make_zip(
        {
            "conversations.json": _fixture(CLAUDE, "conversations.json"),
            "projects.json": _fixture(CLAUDE, "projects.json"),
            "users.json": _fixture(CLAUDE, "users.json"),
        }
    )


def _chatgpt_zip(make_zip: ZipMaker) -> Path:
    return make_zip(
        {
            "conversations.json": _fixture(CHATGPT, "conversations.json"),
            "chat.html": "<html>CHAT HTML</html>",
            "user.json": json.dumps({"id": "user-1", "email": "owner@example.com"}),
            "message_feedback.json": "[]",
            "file-abc-image.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        }
    )


def _by_title(convs: list[Conversation], title: str) -> Conversation:
    return next(c for c in convs if c.title == title)


def _all_text(conv: Conversation) -> str:
    return "\n".join(m.text for m in conv.messages)


# ------------------------------------------------------------------ Claude.ai


def test_claude_export(make_zip: ZipMaker) -> None:
    result = parse_export(_claude_zip(make_zip))
    assert result.format == "claude"
    titles = [c.title for c in result.conversations]
    # oldest first; the empty title is derived from the first user message
    assert titles == [
        "How do I rotate my backup drives safely every month without losing the history?",
        "hi",
        "Planning the garden shed 🌱",
    ]
    assert result.warnings["malformed conversation skipped"] == 2
    assert result.warnings["empty conversation skipped"] == 1
    assert result.warnings["unknown Claude content type 'widget_v9'"] == 1
    assert result.warnings["message from an unknown sender skipped"] == 1
    assert [p.name for p in result.projects] == ["Home improvements"]
    span = result.date_range
    assert span is not None and span[0].year == 2025 and span[1].year == 2026


def test_claude_content_blocks(make_zip: ZipMaker) -> None:
    shed = _by_title(
        parse_export(_claude_zip(make_zip)).conversations, "Planning the garden shed 🌱"
    )
    assert shed.key == "claude-chat:0b6c8d7e-1111-4a4a-9c9c-000000000001"
    assert shed.url.startswith("https://claude.ai/chat/")
    assert [m.role for m in shed.messages] == ["user", "assistant", "user", "user"]
    text = _all_text(shed)
    assert "stale text field" not in text  # content blocks win over `text`
    assert "PRIVATE REASONING" not in text  # thinking is dropped
    assert "RESULT BODY" not in text
    assert "[tool call: web_search]" in text and "[tool result: web_search]" in text
    assert "[widget_v9]" in text and "[image]" in shed.messages[3].text
    assert "Merci ! 日本語 مرحبا" in shed.messages[2].text  # empty content: falls back to text
    atts = {a.name: a.text for a in shed.messages[0].attachments}
    assert atts == {"plan.txt": "Width 3m\nDepth 2m", "photo.jpg": None}
    assert "PROJECT DOC" not in text


def test_claude_pasted_text_is_kept(make_zip: ZipMaker) -> None:
    # Claude.ai stores a paste as an attachment without a file name (and an empty file).
    hi = _by_title(parse_export(_claude_zip(make_zip)).conversations, "hi")
    assert [m.role for m in hi.messages] == ["user", "assistant", "user"]
    paste = hi.messages[2]
    assert paste.text == ""
    assert [(a.name, a.text, a.pasted) for a in paste.attachments] == [
        ("Pasted text", "PASTED ONE", True),
        ("Pasted text 2", "PASTED TWO", True),
    ]
    assert "PASTED TWO" in render(hi).parts[0]


# ------------------------------------------------------------------ ChatGPT


def test_chatgpt_export(make_zip: ZipMaker) -> None:
    result = parse_export(_chatgpt_zip(make_zip))
    assert result.format == "chatgpt"
    assert [c.title for c in result.conversations] == [
        "What's a good sourdough hydration for a beginner loaf baked in a Dutch oven?",
        "Trip to Lisbon",
    ]
    assert result.warnings["malformed conversation skipped"] == 2
    assert result.warnings["conversation without an active branch: newest branch used"] == 1
    assert result.warnings["unknown ChatGPT content type 'tether_browsing_display'"] == 1


def test_chatgpt_active_branch_only(make_zip: ZipMaker) -> None:
    trip = _by_title(parse_export(_chatgpt_zip(make_zip)).conversations, "Trip to Lisbon")
    assert trip.key == "chatgpt-chat:68e1a0b2-aaaa-8000-9000-000000000001"
    text = _all_text(trip)
    for gone in (
        "ABANDONED",
        "You are ChatGPT",
        "CUSTOM INSTRUCTIONS",
        "PRIVATE THOUGHTS",
        "Thought for",
        "TOOL OUTPUT",
        "HIDDEN ASSISTANT TEXT",
        "CHAT HTML",
    ):
        assert gone not in text, gone
    assert [m.role for m in trip.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "assistant",
        "assistant",
    ]
    assert trip.messages[0].attachments[0].name == "itinerary.pdf"
    assert "Olá! 🇵🇹" in trip.messages[0].text
    assert trip.messages[1].text == "Day 1: Alfama.\nDay 2: Belém."
    assert trip.messages[2].text == "[image]\n\nWhat is this building?"
    assert trip.messages[3].text == "```python\nprint('azulejo')\n```"
    assert trip.messages[4].text == "[tether_browsing_display]"
    assert trip.created_at is not None and trip.created_at.year == 2025
    assert trip.updated_at is not None and trip.updated_at > trip.created_at


def test_chatgpt_split_over_files_and_duplicates(make_zip: ZipMaker) -> None:
    convs = json.loads(_fixture(CHATGPT, "conversations.json"))
    newer = dict(convs[1], update_time=1790000000.0, title="Newer copy")
    path = make_zip(
        {
            "conversations-000.json": json.dumps(convs[:1]),
            "part/conversations-001.json": json.dumps(convs[1:]),
            "extra/anything.json": json.dumps([newer]),  # same id, newer: wins
            "user.json": "{}",
        }
    )
    result = parse_export(path)
    assert {c.title for c in result.conversations} == {"Trip to Lisbon", "Newer copy"}
    assert result.warnings["duplicate conversation: newest copy kept"] == 1


def test_mixed_export(make_zip: ZipMaker) -> None:
    path = make_zip(
        {
            "a.json": _fixture(CLAUDE, "conversations.json"),
            "b.json": _fixture(CHATGPT, "conversations.json"),
        }
    )
    result = parse_export(path)
    assert result.format == "mixed"
    assert len(result.conversations) == 5


# ------------------------------------------------------------------ uploads


def test_bare_conversations_json(tmp_path: Path) -> None:
    path = tmp_path / "upload.bin"  # the name does not matter: detection is by structure
    path.write_text(_fixture(CHATGPT, "conversations.json"), encoding="utf-8")
    assert parse_export(path).format == "chatgpt"


def test_wrapped_object_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    data = {"conversations": json.loads(_fixture(CLAUDE, "conversations.json"))}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert parse_export(path).format == "claude"


@pytest.mark.parametrize(
    "content",
    [b"not json at all", b"[]", b'{"id": "user"}', b'[{"foo": 1}]', b"\xff\xfe\x00garbage"],
)
def test_no_conversations_is_an_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "x.json"
    path.write_bytes(content)
    with pytest.raises(ExportError, match=r"No Claude\.ai or ChatGPT conversations"):
        parse_export(path)


@pytest.mark.parametrize(
    "name", ["../evil.json", "/etc/passwd.json", "a/../../b.json", "C:/x.json", "a\\b.json"]
)
def test_unsafe_member_names_reject_the_zip(make_zip: ZipMaker, name: str) -> None:
    assert unsafe_name(name)
    path = make_zip({"conversations.json": _fixture(CLAUDE, "conversations.json"), name: "[]"})
    with pytest.raises(ExportError, match="unsafe file names"):
        parse_export(path)


def test_too_many_entries(make_zip: ZipMaker) -> None:
    path = make_zip({f"img/{i}.png": b"x" for i in range(30)})
    with pytest.raises(ExportError, match="more than 20 entries"):
        parse_export(path, Limits(max_entries=20))


def test_member_size_cap(make_zip: ZipMaker) -> None:
    path = make_zip({"conversations.json": _fixture(CLAUDE, "conversations.json")})
    with pytest.raises(ExportError, match="larger than the import limit"):
        parse_export(path, Limits(max_member_bytes=1000))


def test_total_size_cap(make_zip: ZipMaker) -> None:
    text = _fixture(CHATGPT, "conversations.json")
    path = make_zip({"a.json": text, "b.json": text})
    size = len(text.encode())
    with pytest.raises(ExportError, match="larger than the import limit"):
        parse_export(path, Limits(max_total_bytes=size + 10))


def test_bare_file_size_cap(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(_fixture(CLAUDE, "conversations.json"), encoding="utf-8")
    with pytest.raises(ExportError, match="larger than the import limit"):
        parse_export(path, Limits(max_member_bytes=100))


def test_zip_bomb_ratio(make_zip: ZipMaker) -> None:
    path = make_zip({"conversations.json": "[" + " " * (8 * MiB) + "]"})
    with pytest.raises(ExportError, match="zip bomb"):
        parse_export(path)


def test_corrupt_zip(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 50)
    with pytest.raises(ExportError, match="not a readable zip"):
        parse_export(path)


# ------------------------------------------------------------------ redaction


SECRETS = [
    ("Anthropic API key", f"my key is {ANTHROPIC_KEY} ok"),
    ("OpenAI-style API key", f"OPENAI={OPENAI_KEY}"),
    ("GitHub token", f"token {GITHUB_TOKEN}"),
    ("AWS access key", f"aws {AWS_KEY} here"),
    ("JWT", f"cookie {JWT}"),
    ("private key", f"```\n{PEM}\n```"),
    ("bearer token", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"),
    ("credentials in a URL", "postgres://admin:s3cretpass@db.example.com/app"),
    ("password assignment", "password: hunter2hunter2"),
    ("password assignment", 'api_key = "abcdef1234567890"'),
    ("password assignment", "DB_PASSWORD=correct-horse-battery"),
]


@pytest.mark.parametrize(("kind", "text"), SECRETS)
def test_secrets_are_redacted(kind: str, text: str) -> None:
    result = redact(text)
    assert result.counts[kind] >= 1, result
    assert MARK in result.text
    assert find_secrets(result.text) == [], result.text
    assert redact(result.text).total == 0  # stable: nothing left to find


def test_prose_is_left_alone() -> None:
    text = "We discussed the password policy: rotate often. Tokens expire; keys too."
    assert redact(text).text == text


def test_line_fallback_covers_the_store_check() -> None:
    # wiki-mcp's assignment pattern takes any 8+ non-space characters, commas included.
    text = "keep this\npassword: ab,cdefghijk\nand this"
    result = redact(text)
    assert find_secrets(result.text) == []
    assert result.text.startswith("keep this\n") and result.text.endswith("\nand this")


# ------------------------------------------------------------------ transcripts


def test_transcript_of_a_claude_chat(make_zip: ZipMaker) -> None:
    shed = _by_title(
        parse_export(_claude_zip(make_zip)).conversations, "Planning the garden shed 🌱"
    )
    out = render(shed)
    assert len(out.parts) == 1
    md = out.parts[0]
    assert md.startswith("# Planning the garden shed 🌱\n")
    assert "- Conversation id: `claude-chat:0b6c8d7e-1111-4a4a-9c9c-000000000001`" in md
    assert "- Started: 2026-01-10 09:00 UTC" in md
    assert "## User · 2026-01-10 09:00 UTC" in md and "## Assistant ·" in md
    assert "#### Shed plan" in md  # demoted below the speaker headings
    assert "hunter2" not in md and out.redactions["password assignment"] == 1
    assert "**Attachment:** plan.txt" in md and "Width 3m" in md
    assert find_secrets(md) == []


def test_huge_code_is_truncated() -> None:
    code = "\n".join(f"line {i}" for i in range(500))
    text, n = tidy_markdown(f"before\n```python\n{code}\n```\n# after")
    assert n == 1
    assert "line 29" in text and "line 30" not in text
    assert "470 more lines truncated" in text
    assert text.endswith("```\n#### after")


def test_unclosed_fence_is_closed() -> None:
    text, _ = tidy_markdown("```\ncode\n# not a heading")
    assert text == "```\ncode\n# not a heading\n```"


def test_fenced_code_cannot_break_out() -> None:
    block = fenced("a\n```\n# injected heading\n```")
    assert block.startswith("````") and block.endswith("````")


def test_large_conversation_is_split_into_parts() -> None:
    big = "word " * 60_000  # ~300 KB per message
    messages = tuple(Message("user" if i % 2 == 0 else "assistant", big) for i in range(8))
    conv = Conversation("chatgpt", "big", f"Big {OPENAI_KEY}", None, None, messages)
    out = render(conv)
    assert len(out.parts) >= 3
    for i, part in enumerate(out.parts, 1):
        assert len(part.encode()) <= MAX_PART_BYTES
        assert f"- Part {i} of {len(out.parts)}" in part
        assert OPENAI_KEY not in part and find_secrets(part) == []


def test_single_giant_message_is_capped() -> None:
    conv = Conversation("claude", "x", "t", None, None, (Message("user", "é" * 2_000_000),))
    out = render(conv)
    assert len(out.parts) == 1
    assert len(out.parts[0].encode()) <= MAX_PART_BYTES
    assert "message truncated on import" in out.parts[0]


def test_pastes_keep_more_text_than_uploads() -> None:
    body = "x" * 10_000
    msg = Message(
        "user",
        "",
        attachments=(Attachment("notes.txt", body), Attachment("Pasted text", body, pasted=True)),
    )
    out = render(Conversation("claude", "p", "t", None, None, (msg,)))
    assert out.truncated_blocks == 1  # only the upload
    assert body in out.parts[0]


def test_derive_title() -> None:
    long = Message("user", "  " + "word " * 40)
    assert derive_title([long]).endswith("…") and len(derive_title([long])) <= 81
    assert derive_title([Message("assistant", "hi")]) == "Untitled conversation"

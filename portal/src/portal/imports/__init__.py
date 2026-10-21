"""Import chat exports from Claude.ai and ChatGPT into the wiki.

parse_export() reads an uploaded export (a zip or a bare conversations.json) without
extracting it, detects each JSON member's format by its structure and normalises
every conversation into portal.imports.model. Nothing here talks to the network.
"""

from portal.imports.model import Attachment, Conversation, ExportError, Message, ParseResult
from portal.imports.parse import parse_export

__all__ = [
    "Attachment",
    "Conversation",
    "ExportError",
    "Message",
    "ParseResult",
    "parse_export",
]

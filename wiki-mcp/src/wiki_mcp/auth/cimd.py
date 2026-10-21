"""Fetch OAuth Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document).

The client_id is an HTTPS URL. The provider only calls this for hosts on the
CIMD_ALLOWED_HOSTS allowlist; this module adds SSRF guards on top: https only,
default port, no redirects, public IPs only, small size and time limits.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx2

MAX_BYTES = 16 * 1024
TIMEOUT_S = 5.0


class CIMDError(ValueError):
    pass


async def _resolve_public(host: str) -> None:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise CIMDError(f"{host} resolves to a non-public address")


async def fetch_client_metadata(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443):
        raise CIMDError("client_id must be an https URL on the default port")
    if parts.username or parts.password or parts.fragment:
        raise CIMDError("client_id URL must not contain credentials or a fragment")
    await _resolve_public(parts.hostname)
    async with (
        httpx2.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client,
        client.stream("GET", url, headers={"Accept": "application/json"}) as response,
    ):
        if response.status_code != 200:
            raise CIMDError(f"metadata document returned HTTP {response.status_code}")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise CIMDError("metadata document too large")
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CIMDError("metadata document is not JSON") from exc
    if not isinstance(doc, dict):
        raise CIMDError("metadata document must be a JSON object")
    return doc

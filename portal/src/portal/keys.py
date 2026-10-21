"""Provider API keys: registry, storage, masking and validation.

Keys live in one file, KEYS_DIR/keys.env (KEY=value lines, mode 600), read by the reload
supervisors of LiteLLM (model keys) and LibreChat (web search keys) and by refresh_models.py. Next to it, keys.meta.json holds what the
portal may show: the masked hint, a short fingerprint and when the key was added and
last tested. A key value never leaves this module towards the browser or the logs.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx2
import yaml

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
# Printable, no whitespace, quotes, `$`, `#` or backslash: safe in the env file as-is.
KEY_CHARS = re.compile(r"^[A-Za-z0-9_\-.:+/=]{16,512}$")


class KeyFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    env: str
    litellm_prefix: str
    # Model providers: lists the models (and validates the key). Search services: validates
    # the key; with check_body it is POSTed, otherwise fetched with GET.
    models_url: str
    auth: Literal["bearer", "x-api-key"] = "bearer"
    headers: dict[str, str] = field(default_factory=dict)
    key_prefix: str = ""
    keys_url: str = ""
    kind: Literal["model", "search"] = "model"
    check_body: dict[str, Any] | None = None
    purpose: str = ""


def load_providers(path: Path) -> dict[str, Provider]:
    """config/providers.yaml, validated (a typo must not become a strange env var or URL)."""
    raw = (yaml.safe_load(path.read_text()) or {}).get("providers") or {}
    out: dict[str, Provider] = {}
    for name, p in raw.items():
        if not _NAME.match(str(name)):
            raise ValueError(f"providers.yaml: invalid provider name {name!r}")
        env = str(p.get("env", ""))
        if not _ENV.match(env):
            raise ValueError(f"providers.yaml: {name}.env must look like NAME_API_KEY")
        kind = str(p.get("kind", "model"))
        if kind not in ("model", "search"):
            raise ValueError(f"providers.yaml: {name}.kind must be model or search")
        url_field = "models_url" if kind == "model" else "check_url"
        url = str(p.get(url_field, ""))
        if not url.startswith("https://"):
            raise ValueError(f"providers.yaml: {name}.{url_field} must be https")
        body = p.get("check_body")
        if body is not None and not isinstance(body, dict):
            raise ValueError(f"providers.yaml: {name}.check_body must be a mapping")
        auth = str(p.get("auth", "bearer"))
        if auth not in ("bearer", "x-api-key"):
            raise ValueError(f"providers.yaml: {name}.auth must be bearer or x-api-key")
        keys_url = str(p.get("keys_url", ""))
        out[name] = Provider(
            name=name,
            label=str(p.get("label", name)),
            env=env,
            litellm_prefix=str(p.get("litellm_prefix", name)),
            models_url=url,
            auth=auth,  # type: ignore[arg-type]
            headers={str(k): str(v) for k, v in (p.get("headers") or {}).items()},
            key_prefix=str(p.get("key_prefix", "")),
            keys_url=keys_url if keys_url.startswith("https://") else "",
            kind=kind,  # type: ignore[arg-type]
            check_body=body,
            purpose=str(p.get("purpose", "")),
        )
    return out


def clean_key(value: str) -> str:
    key = value.strip()
    if not KEY_CHARS.match(key):
        raise KeyFormatError(
            "That does not look like an API key (16-512 characters, no spaces or quotes)."
        )
    return key


def mask(provider: Provider, key: str) -> str:
    prefix = (
        provider.key_prefix if provider.key_prefix and key.startswith(provider.key_prefix) else ""
    )
    return f"{prefix}…{key[-4:]}"


def fingerprint(key: str) -> str:
    """Short, non-reversible identifier for audit entries (never the key itself)."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


@dataclass
class KeyStatus:
    provider: Provider
    configured: bool
    hint: str = ""
    added: str = ""
    tested: str = ""
    test_ok: bool | None = None
    test_detail: str = ""


class KeyStore:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.file = directory / "keys.env"
        self.meta_file = directory / "keys.meta.json"

    def _lines(self) -> list[str]:
        return self.file.read_text().splitlines() if self.file.exists() else []

    def read(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self._lines():
            m = _LINE.match(line.strip())
            if m and m.group(2):
                out[m.group(1)] = m.group(2)
        return out

    def get(self, provider: Provider) -> str | None:
        return self.read().get(provider.env)

    def _meta(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.meta_file.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        _write_private(self.meta_file, json.dumps(meta, indent=2, sort_keys=True) + "\n")

    def _write_keys(self, env: str, value: str | None) -> None:
        """Replace or drop one line; every other line (other providers, comments) stays."""
        lines = [ln for ln in self._lines() if not ln.strip().startswith(f"{env}=")]
        if value is not None:
            lines.append(f"{env}={value}")
        _write_private(self.file, "".join(f"{ln}\n" for ln in lines))

    def save(self, provider: Provider, key: str, *, detail: str) -> bool:
        """Store a key that passed validation. Returns True if it replaced one."""
        replaced = provider.env in self.read()
        self._write_keys(provider.env, key)
        meta = self._meta()
        now = _now()
        meta[provider.name] = {
            "hint": mask(provider, key),
            "fingerprint": fingerprint(key),
            "added": now,
            "tested": now,
            "test_ok": True,
            "test_detail": detail,
        }
        self._save_meta(meta)
        return replaced

    def remove(self, provider: Provider) -> str | None:
        """Drop the key; returns its fingerprint (for the audit log), or None if unset."""
        key = self.get(provider)
        if key is None:
            return None
        self._write_keys(provider.env, None)
        meta = self._meta()
        meta.pop(provider.name, None)
        self._save_meta(meta)
        return fingerprint(key)

    def record_test(self, provider: Provider, ok: bool, detail: str) -> None:
        meta = self._meta()
        entry = meta.setdefault(provider.name, {})
        entry.update({"tested": _now(), "test_ok": ok, "test_detail": detail})
        self._save_meta(meta)

    def status(self, providers: dict[str, Provider]) -> list[KeyStatus]:
        keys, meta = self.read(), self._meta()
        out = []
        for p in providers.values():
            key = keys.get(p.env)
            m = meta.get(p.name, {}) if key else {}
            out.append(
                KeyStatus(
                    provider=p,
                    configured=key is not None,
                    # a key added outside the portal (e.g. migrated from .env) has no metadata
                    hint=str(m.get("hint") or (mask(p, key) if key else "")),
                    added=str(m.get("added", "")),
                    tested=str(m.get("tested", "")),
                    test_ok=m.get("test_ok"),
                    test_detail=str(m.get("test_detail", "")),
                )
            )
        return out


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


async def check_key(
    provider: Provider,
    key: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    http_timeout: float = 15.0,
) -> CheckResult:
    """Ask the provider (its /models endpoint, or a search service's check URL) whether it
    accepts the key."""
    headers = dict(provider.headers)
    if provider.auth == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx2.AsyncClient(timeout=http_timeout, transport=transport) as http:
            if provider.check_body is None:
                r = await http.get(provider.models_url, headers=headers)
            else:
                r = await http.post(provider.models_url, headers=headers, json=provider.check_body)
    except httpx2.TimeoutException:
        return CheckResult(False, f"{provider.label} did not answer in time")
    except httpx2.HTTPError as exc:
        return CheckResult(False, f"could not reach {provider.label} ({type(exc).__name__})")
    if r.status_code in (401, 403):
        return CheckResult(False, f"{provider.label} rejected the key (HTTP {r.status_code})")
    if r.status_code == 429:
        return CheckResult(False, f"{provider.label} is rate limiting this key (HTTP 429)")
    if r.status_code != 200:
        return CheckResult(False, f"{provider.label} answered HTTP {r.status_code}")
    count: int | None = None
    if provider.kind == "model":
        with contextlib.suppress(ValueError, AttributeError):
            count = len(r.json().get("data") or [])
    return CheckResult(
        True, f"accepted by {provider.label}" + (f" ({count} models)" if count else "")
    )

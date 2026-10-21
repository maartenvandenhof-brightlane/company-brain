"""Chat completions through LiteLLM (OpenAI-compatible), the only way the portal calls a
model. Every call is logged by LiteLLM's spend log like any chat, so it shows on the Usage
page. JSON mode and temperature 0: extraction, not conversation. GLM always thinks and its
LiteLLM alias defaults to low effort for chat; these unattended calls ask for high. Other
models get no reasoning_effort (Claude and GPT would take it and start thinking).

A client with a `meter` (dreams and ideas: portal.dreams.budget) asks it before each call,
which may refuse the call (BudgetRefused), and tells it what the call used afterwards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

BATCH_EFFORT = "high"


class LLMError(RuntimeError):
    """The model call failed or answered with something unusable."""


class BudgetRefused(LLMError):
    """The budget guard refused the call before it was made."""


class Meter(Protocol):
    def before(self, model: str, chars: int, max_tokens: int | None) -> None:
        """Raise BudgetRefused when the call may not be made."""

    def after(self, model: str, input_tokens: int, output_tokens: int) -> None: ...


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class LLM:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        timeout: float = 300.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
            transport=transport,
        )
        self.usage = Usage()
        self.meter: Meter | None = None

    async def close(self) -> None:
        await self.http.aclose()

    async def json_chat(
        self, model: str, messages: list[dict[str, str]], *, max_tokens: int | None = None
    ) -> str:
        """The reply text of one call that asks for a JSON object (at most `max_tokens`)."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if is_glm(model):
            payload["reasoning_effort"] = BATCH_EFFORT
        if self.meter is not None:
            self.meter.before(model, sum(len(m.get("content", "")) for m in messages), max_tokens)
        try:
            r = await self.http.post("/v1/chat/completions", json=payload)
        except httpx2.HTTPError as exc:
            raise LLMError(f"LiteLLM unreachable ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise LLMError(f"LiteLLM answered HTTP {r.status_code}")
        try:
            body = r.json()
            text = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("LiteLLM answered without a message") from exc
        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        self.usage.calls += 1
        self.usage.input_tokens += tokens_in
        self.usage.output_tokens += tokens_out
        if self.meter is not None:
            self.meter.after(model, tokens_in, tokens_out)
        if not isinstance(text, str):
            raise LLMError("LiteLLM answered without text")
        return text


def is_glm(model: str) -> bool:
    """A GLM model: the glm-5-3 alias or a listed id such as mistral/zai-glm-5-3."""
    return "glm" in model.rsplit("/", 1)[-1].lower()

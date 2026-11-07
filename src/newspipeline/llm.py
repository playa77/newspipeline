"""Single LLM call path: OpenAI-compatible chat completions client.

All LLM traffic in newspipeline goes through LLMClient — there is no other
code path. Default provider is OpenRouter; both endpoint and model are
configurable via settings.toml.

OpenCode Go endpoints (https://opencode.ai/zen/go/v1) additionally require a
stable ``x-opencode-session`` header per conversation and a client-specific
User-Agent (docs/console/go). One session id is generated per LLMClient
instance, i.e. per pipeline run. Reasoning effort is sent in the OpenAI-native
``reasoning_effort`` string form there; the OpenRouter ``reasoning: {effort}``
object remains the default for other endpoints.

The API key is used only for the Authorization header and is never logged
(invariant G1).
"""

# version: 0.2.1

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from newspipeline import __version__

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 5


class LLMError(Exception):
    """Raised when a chat completion fails after all retries."""


class LLMClient:
    def __init__(self, *, api_key: str, base_url: str, default_model: str) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        host = (urlparse(self._base_url).hostname or "").lower()
        self._is_opencode = host in ("opencode.ai", "www.opencode.ai")
        headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
        if self._is_opencode:
            headers["x-opencode-session"] = str(uuid.uuid4())
            headers["User-Agent"] = f"newspipeline/{__version__}"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0), headers=headers)

    async def close(self) -> None:
        await self._http.aclose()

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.5,
        max_tokens: int = 2000,
        reasoning_effort: str | None = None,
    ) -> str:
        """One chat completion call with retries. Returns the message content.

        ``reasoning_effort`` is forwarded as ``reasoning: {effort: ...}``
        (OpenRouter/OpenAI-compatible convention) only when set — providers
        that do not support reasoning never see the parameter.
        """
        use_model = model or self._default_model
        body: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if reasoning_effort:
            if self._is_opencode:
                body["reasoning_effort"] = reasoning_effort
            else:
                body["reasoning"] = {"effort": reasoning_effort}

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._http.post(f"{self._base_url}/chat/completions", json=body)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"transient HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not content:
                    # Reasoning models with a mandatory thinking phase (e.g.
                    # glm-5.3-flash at effort "max") can exhaust max_tokens on
                    # reasoning alone and answer HTTP 200 with content=null.
                    # That is a retryable upstream failure, not a result.
                    raise LLMError(
                        "upstream returned null/empty message content "
                        "(max_tokens likely consumed by reasoning tokens)"
                    )
                return content
            except (LLMError, httpx.HTTPError, KeyError, IndexError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(
                        "LLM call failed (attempt %d/%d, model=%s) — retrying in %ds: %s",
                        attempt + 1, _MAX_RETRIES + 1, use_model, delay, exc,
                    )
                    await asyncio.sleep(delay)

        raise LLMError(f"LLM call failed after {_MAX_RETRIES + 1} attempts: {last_exc}")

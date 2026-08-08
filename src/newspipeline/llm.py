"""Single LLM call path: OpenAI-compatible chat completions client.

All LLM traffic in newspipeline goes through LLMClient — there is no other
code path. Default provider is OpenRouter; both endpoint and model are
configurable via settings.toml.

The API key is used only for the Authorization header and is never logged
(invariant G1).
"""

# version: 0.1.0

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

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
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )

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
            body["reasoning"] = {"effort": reasoning_effort}

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._http.post(f"{self._base_url}/chat/completions", json=body)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"transient HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
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

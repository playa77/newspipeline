"""G10 — LLM null/empty content must not be returned as a success.

Real-world trigger: reasoning models with a mandatory reasoning phase (e.g.
z-ai/glm-5.3-flash at default effort ``max``) can burn the entire
``max_tokens`` budget on reasoning tokens and return HTTP 200 with
``message.content = null``. Returning that null as a successful string sent
``_extract_json_array(None)`` down a ``'NoneType' object has no attribute
strip'`` path (journal: "Analysis failed for run 2"), the run "completed"
with zero themes, zero deliverables — and zero emails. The client must treat
a null/empty content like any other transient upstream failure and retry.
"""

# version: 0.1.0

import asyncio
import json

import pytest

from newspipeline.llm import LLMClient, LLMError


class _FakeHTTP:
    """Minimal stand-in for the httpx.AsyncClient used by LLMClient."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = 0

    def post(self, *_args, **_kwargs):
        return _AwaitableResponse(self)


class _AwaitableResponse:
    """Awaits like a coroutine (httpx.AsyncClient.post is async)."""

    def __init__(self, owner: _FakeHTTP):
        self._owner = owner

    def __await__(self):
        # Yield once so asyncio.run's loop actually awaits it.
        return self._response().__await__()

    async def _response(self):
        await asyncio.sleep(0)
        return _FakeResponse(self._owner)


class _FakeResponse:
    def __init__(self, owner: _FakeHTTP):
        self._owner = owner

    @property
    def status_code(self) -> int:
        return 200

    @property
    def text(self) -> str:
        return ""

    def raise_for_status(self) -> None:
        return None

    def json(self):
        payload = self._owner.payloads[min(self._owner.calls, len(self._owner.payloads) - 1)]
        self._owner.calls += 1
        return payload


def _client_with(monkeypatch, payloads) -> tuple[LLMClient, _FakeHTTP]:
    client = LLMClient(api_key="k", base_url="http://x", default_model="m")
    fake = _FakeHTTP(payloads)
    monkeypatch.setattr(client, "_http", fake)
    return client, fake


def test_null_content_is_retried_and_then_raises(monkeypatch):
    """content=None must never be returned as success: retry, then fail loudly."""
    client, fake = _client_with(
        monkeypatch,
        [{"choices": [{"message": {"role": "assistant", "content": None}}]}] * 3,
    )
    with pytest.raises(LLMError):
        asyncio.run(client.chat_completion(messages=[{"role": "user", "content": "hi"}]))
    assert fake.calls == 3  # initial + 2 retries


def test_null_content_then_valid_content_recovers(monkeypatch):
    """A null-content response followed by a good one must recover via retry."""
    client, _ = _client_with(
        monkeypatch,
        [
            {"choices": [{"message": {"role": "assistant", "content": None}}]},
            {"choices": [{"message": {"role": "assistant", "content": "[]"}}]},
        ],
    )
    result = asyncio.run(client.chat_completion(messages=[{"role": "user", "content": "hi"}]))
    assert result == "[]"


def test_empty_string_content_is_retried_and_then_raises(monkeypatch):
    """An empty string content is equally useless — same contract as null."""
    client, fake = _client_with(
        monkeypatch,
        [{"choices": [{"message": {"role": "assistant", "content": ""}}]}] * 3,
    )
    with pytest.raises(LLMError):
        asyncio.run(client.chat_completion(messages=[{"role": "user", "content": "hi"}]))
    assert fake.calls == 3


def test_reasoning_tokens_only_response_is_survivable(monkeypatch):
    """A payload shaped like glm-5.3-flash's exhausted-reasoning answer must
    not raise in the client — it retries, then raises LLMError (null content)."""
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "Long thinking...",
                }
            }
        ],
        "usage": {
            "completion_tokens": 2000,
            "completion_tokens_details": {"reasoning_tokens": 2000},
        },
    }
    client, _ = _client_with(monkeypatch, [body] * 3)
    with pytest.raises(LLMError):
        asyncio.run(client.chat_completion(messages=[{"role": "user", "content": "hi"}]))

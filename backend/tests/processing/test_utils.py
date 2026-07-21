"""Tests for the shared LLM processing helpers.

All external calls are mocked — ``anthropic.AsyncAnthropic`` is patched on
the anthropic module so ``call_claude`` constructs a MagicMock client. No
real API calls, no real sleeps.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest
from anthropic.types import MessageParam

from config import settings
from processing.utils import DEFAULT_MAX_TOKENS, call_claude, parse_json_object

MESSAGES: list[MessageParam] = [{"role": "user", "content": "Extract the features."}]


def _make_response(response_text: str) -> MagicMock:
    """Build a mock API response whose first content block is ``response_text``."""
    block = MagicMock()
    block.type = "text"
    block.text = response_text
    response = MagicMock()
    response.content = [block]
    return response


def _make_client(response_text: str) -> MagicMock:
    """Build a mock Anthropic client whose response contains ``response_text``."""
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_response(response_text))
    return client


def _make_failing_client(error: Exception) -> MagicMock:
    """Build a mock Anthropic client whose API call always raises ``error``."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=error)
    return client


def _install_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    """Route ``call_claude``'s client construction to the given mock."""
    monkeypatch.setattr(anthropic, "AsyncAnthropic", MagicMock(return_value=client))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace asyncio.sleep so retry backoff runs instantly."""
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return sleep


class TestCallClaude:
    """call_claude constructs a client, retries, and returns the text."""

    async def test_call_claude_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful call returns the response text."""
        client = _make_client("the response text")
        _install_client(monkeypatch, client)

        result = await call_claude(MESSAGES)

        assert result == "the response text"
        client.messages.create.assert_awaited_once()
        assert client.messages.create.await_args.kwargs["messages"] == MESSAGES

    async def test_call_claude_retries_on_error(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """Transient API errors are retried with exponential backoff."""
        client = MagicMock()
        client.messages.create = AsyncMock(
            side_effect=[
                anthropic.AnthropicError("server overloaded"),
                anthropic.AnthropicError("server overloaded"),
                _make_response("recovered"),
            ]
        )
        _install_client(monkeypatch, client)

        result = await call_claude(MESSAGES)

        assert result == "recovered"
        assert client.messages.create.await_count == 3
        assert [call.args[0] for call in no_sleep.await_args_list] == [1, 2]

    async def test_call_claude_returns_none_after_exhausted(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """A persistently failing API call returns None after all retries."""
        client = _make_failing_client(anthropic.AnthropicError("server overloaded"))
        _install_client(monkeypatch, client)

        assert await call_claude(MESSAGES) is None
        assert client.messages.create.await_count == settings.MAX_RETRIES + 1

    async def test_call_claude_uses_sonnet_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without overrides the call uses Sonnet and the default max_tokens."""
        client = _make_client("ok")
        _install_client(monkeypatch, client)

        await call_claude(MESSAGES)

        call_kwargs = client.messages.create.await_args.kwargs
        assert call_kwargs["model"] == settings.SONNET_MODEL
        assert call_kwargs["max_tokens"] == DEFAULT_MAX_TOKENS

    async def test_call_claude_accepts_custom_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit model and max_tokens overrides are passed through."""
        client = _make_client("ok")
        _install_client(monkeypatch, client)

        await call_claude(MESSAGES, model=settings.OPUS_MODEL, max_tokens=1024)

        call_kwargs = client.messages.create.await_args.kwargs
        assert call_kwargs["model"] == settings.OPUS_MODEL
        assert call_kwargs["max_tokens"] == 1024

    async def test_call_claude_returns_none_on_empty_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response with no content blocks returns None without retrying."""
        response = MagicMock()
        response.content = []
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=response)
        _install_client(monkeypatch, client)

        assert await call_claude(MESSAGES) is None
        client.messages.create.assert_awaited_once()

    async def test_call_claude_returns_none_on_non_text_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response whose first block is not text returns None."""
        block = MagicMock()
        block.type = "tool_use"
        response = MagicMock()
        response.content = [block]
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=response)
        _install_client(monkeypatch, client)

        assert await call_claude(MESSAGES) is None


class TestParseJsonObject:
    """parse_json_object parses clean or wrapped JSON objects."""

    def test_parses_clean_json_object(self) -> None:
        """A plain JSON object parses to a dict."""
        payload = {"headline": "BYD launches City NOA", "relevance_score": 8}

        assert parse_json_object(json.dumps(payload)) == payload

    def test_salvages_json_wrapped_in_text(self) -> None:
        """JSON embedded in preamble and trailing text is salvaged."""
        payload = {"headline": "BYD launches City NOA"}
        text = f"Here is the extraction:\n{json.dumps(payload)}\nDone."

        assert parse_json_object(text) == payload

    def test_returns_none_for_non_json_text(self) -> None:
        """Text with no JSON object returns None."""
        assert parse_json_object("I could not process this article, sorry.") is None

    def test_returns_none_for_unparseable_braces(self) -> None:
        """A brace-wrapped slice that still fails to parse returns None."""
        assert parse_json_object("{this is not valid json}") is None

    def test_returns_none_for_non_object_json(self) -> None:
        """Valid JSON that is not an object (e.g. an array) returns None."""
        assert parse_json_object('["a", "b"]') is None

"""Tests for the shared LLM processing helpers.

All external calls are mocked — ``anthropic.AsyncAnthropic`` is patched on
the anthropic module so ``call_claude`` and the batch helpers construct a
MagicMock client. No real API calls, no real sleeps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest
from anthropic import omit
from anthropic.types import MessageParam
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from config import settings
from processing.utils import (
    BATCH_POLL_INTERVAL_SECONDS,
    DEFAULT_MAX_TOKENS,
    call_claude,
    get_batch_results,
    parse_json_object,
    poll_batch,
    submit_batch,
)

MESSAGES: list[MessageParam] = [{"role": "user", "content": "Extract the features."}]

BATCH_REQUESTS: list[Request] = [
    Request(
        custom_id="doc-1",
        params=MessageCreateParamsNonStreaming(
            model=settings.SONNET_MODEL,
            max_tokens=64,
            messages=[{"role": "user", "content": "Extract the features."}],
        ),
    )
]


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


def _make_batch(
    status: str,
    *,
    batch_id: str = "batch_123",
    processing: int = 0,
    succeeded: int = 0,
    errored: int = 0,
    canceled: int = 0,
    expired: int = 0,
) -> MagicMock:
    """Build a mock MessageBatch with the given status and request counts."""
    batch = MagicMock()
    batch.id = batch_id
    batch.processing_status = status
    batch.request_counts.processing = processing
    batch.request_counts.succeeded = succeeded
    batch.request_counts.errored = errored
    batch.request_counts.canceled = canceled
    batch.request_counts.expired = expired
    return batch


def _make_batches_client() -> MagicMock:
    """Build a mock Anthropic client exposing async messages.batches methods."""
    client = MagicMock()
    client.messages.batches.create = AsyncMock()
    client.messages.batches.retrieve = AsyncMock()
    client.messages.batches.results = AsyncMock()
    return client


def _entry(custom_id: str, *, result_type: str = "succeeded", text: str = "") -> MagicMock:
    """Build a mock batch result entry for one custom_id."""
    entry = MagicMock()
    entry.custom_id = custom_id
    entry.result.type = result_type
    if result_type == "succeeded":
        block = MagicMock()
        block.type = "text"
        block.text = text
        entry.result.message.content = [block]
    return entry


async def _aiter(entries: list[MagicMock]) -> AsyncIterator[MagicMock]:
    """Yield entries as an async iterator, mirroring the results decoder."""
    for entry in entries:
        yield entry


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

    async def test_call_claude_omits_system_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a system prompt the system parameter stays omitted."""
        client = _make_client("ok")
        _install_client(monkeypatch, client)

        await call_claude(MESSAGES)

        assert client.messages.create.await_args.kwargs["system"] is omit

    async def test_call_claude_sends_cached_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A system prompt is sent as one text block with a cache breakpoint."""
        client = _make_client("ok")
        _install_client(monkeypatch, client)

        await call_claude(MESSAGES, system="You are an analyst.")

        assert client.messages.create.await_args.kwargs["system"] == [
            {
                "type": "text",
                "text": "You are an analyst.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

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


class TestSubmitBatch:
    """submit_batch creates a batch and returns its ID."""

    async def test_submit_batch_returns_batch_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful submission returns the batch ID from the API."""
        client = _make_batches_client()
        client.messages.batches.create.return_value = _make_batch("in_progress")
        _install_client(monkeypatch, client)

        batch_id = await submit_batch(BATCH_REQUESTS)

        assert batch_id == "batch_123"
        client.messages.batches.create.assert_awaited_once_with(requests=BATCH_REQUESTS)

    async def test_submit_batch_propagates_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Submission failures propagate so the pipeline can fall back to sync."""
        client = _make_batches_client()
        client.messages.batches.create.side_effect = anthropic.AnthropicError("api down")
        _install_client(monkeypatch, client)

        with pytest.raises(anthropic.AnthropicError):
            await submit_batch(BATCH_REQUESTS)


class TestPollBatch:
    """poll_batch polls until the batch ends or the timeout elapses."""

    async def test_poll_batch_returns_ended_batch(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """Polling continues past in_progress and returns the ended batch."""
        client = _make_batches_client()
        ended = _make_batch("ended", succeeded=2)
        client.messages.batches.retrieve.side_effect = [
            _make_batch("in_progress", processing=2),
            ended,
        ]
        _install_client(monkeypatch, client)

        result = await poll_batch("batch_123")

        assert result is ended
        assert client.messages.batches.retrieve.await_count == 2
        no_sleep.assert_awaited_once_with(BATCH_POLL_INTERVAL_SECONDS)

    async def test_poll_batch_returns_immediately_when_ended(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """An already-ended batch returns without any sleep."""
        client = _make_batches_client()
        client.messages.batches.retrieve.return_value = _make_batch("ended", succeeded=1)
        _install_client(monkeypatch, client)

        result = await poll_batch("batch_123")

        assert result is not None
        no_sleep.assert_not_awaited()

    async def test_poll_batch_logs_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_sleep: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Each poll logs succeeded/total request counts."""
        client = _make_batches_client()
        client.messages.batches.retrieve.return_value = _make_batch(
            "ended", succeeded=2, errored=1, processing=2
        )
        _install_client(monkeypatch, client)

        with caplog.at_level("INFO"):
            await poll_batch("batch_123")

        assert "Batch batch_123: 2/5 complete" in caplog.text

    async def test_poll_batch_times_out_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """A batch that never ends returns None after timeout_seconds of polls."""
        client = _make_batches_client()
        client.messages.batches.retrieve.return_value = _make_batch("in_progress", processing=3)
        _install_client(monkeypatch, client)

        result = await poll_batch("batch_123", poll_interval_seconds=30, timeout_seconds=90)

        assert result is None
        assert client.messages.batches.retrieve.await_count == 3

    async def test_poll_batch_survives_transient_retrieve_error(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """A transient retrieve error is logged and polling continues."""
        client = _make_batches_client()
        ended = _make_batch("ended", succeeded=1)
        client.messages.batches.retrieve.side_effect = [
            anthropic.AnthropicError("server overloaded"),
            ended,
        ]
        _install_client(monkeypatch, client)

        result = await poll_batch("batch_123")

        assert result is ended


class TestGetBatchResults:
    """get_batch_results maps custom_ids to response text."""

    async def test_get_batch_results_maps_custom_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Results arriving in any order are keyed by custom_id."""
        client = _make_batches_client()
        client.messages.batches.results.return_value = _aiter(
            [_entry("doc-2", text="second"), _entry("doc-1", text="first")]
        )
        _install_client(monkeypatch, client)

        results = await get_batch_results("batch_123")

        assert results == {"doc-1": "first", "doc-2": "second"}

    async def test_get_batch_results_none_for_errored_and_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-succeeded results map to None."""
        client = _make_batches_client()
        client.messages.batches.results.return_value = _aiter(
            [
                _entry("doc-1", text="ok"),
                _entry("doc-2", result_type="errored"),
                _entry("doc-3", result_type="expired"),
            ]
        )
        _install_client(monkeypatch, client)

        results = await get_batch_results("batch_123")

        assert results == {"doc-1": "ok", "doc-2": None, "doc-3": None}

    async def test_get_batch_results_none_for_non_text_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A succeeded result whose first block is not text maps to None."""
        entry = _entry("doc-1", text="ignored")
        entry.result.message.content[0].type = "tool_use"
        client = _make_batches_client()
        client.messages.batches.results.return_value = _aiter([entry])
        _install_client(monkeypatch, client)

        results = await get_batch_results("batch_123")

        assert results == {"doc-1": None}

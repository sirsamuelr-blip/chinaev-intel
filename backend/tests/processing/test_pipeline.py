"""Tests for the LLM extraction pipeline.

All external calls are mocked — the Anthropic client is a MagicMock with an
async ``messages.create`` (installed by patching ``anthropic.AsyncAnthropic``,
which the shared ``processing.utils.call_claude`` helper constructs), and
every Firestore helper is monkeypatched on the pipeline module. No real API
calls, no real database calls, no real sleeps.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from config import settings
from processing import pipeline
from processing.prompts import EXTRACTION_PROMPT, build_extraction_message

SAMPLE_ARTICLE = {
    "id": "test-doc-123",
    "source_name": "gasgoo",
    "source_url": "https://autonews.gasgoo.com/Detail/2024001",
    "title": "BYD Launches New ADAS Feature for Han EV",
    "body": "BYD announced today that its Han EV sedan will receive...",
    "publish_date": "2026-07-15T08:00:00+00:00",
    "scrape_date": "2026-07-15T12:00:00+00:00",
    "language": "en",
}

SAMPLE_EXTRACTION = {
    "headline": "BYD Launches New ADAS Feature for Han EV",
    "summary": "BYD has launched a new ADAS feature...",
    "relevance_score": 8,
    "brands_mentioned": ["BYD"],
    "vehicles_mentioned": ["Han EV"],
    "features_extracted": [
        {
            "feature_name": "City NOA",
            "category": "adas",
            "description": "Navigate on autopilot for city streets",
            "supplier": None,
            "is_new": True,
        }
    ],
    "competitive_signal": "BYD's rapid ADAS rollout...",
    "content_type": "news",
}


def _make_client(response_text: str) -> MagicMock:
    """Build a mock Anthropic client whose response contains ``response_text``."""
    block = MagicMock()
    block.type = "text"
    block.text = response_text
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _make_failing_client(error: Exception) -> MagicMock:
    """Build a mock Anthropic client whose API call always raises ``error``."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=error)
    return client


def _install_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    """Route ``processing.utils.call_claude``'s client construction to ``client``."""
    monkeypatch.setattr(anthropic, "AsyncAnthropic", MagicMock(return_value=client))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace asyncio.sleep so retry backoff runs instantly."""
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return sleep


class TestPrompts:
    """The extraction prompt template and message builder."""

    def test_extraction_prompt_is_string(self) -> None:
        """EXTRACTION_PROMPT is a non-empty string."""
        assert isinstance(EXTRACTION_PROMPT, str)
        assert len(EXTRACTION_PROMPT) > 0

    def test_extraction_prompt_requests_json(self) -> None:
        """The prompt instructs Claude to respond in JSON."""
        assert "json" in EXTRACTION_PROMPT.lower()

    def test_build_extraction_message_includes_prompt(self) -> None:
        """The combined message contains the full extraction prompt."""
        message = build_extraction_message("A title", "A body")
        assert EXTRACTION_PROMPT in message

    def test_build_extraction_message_includes_article(self) -> None:
        """The combined message contains the article title and body."""
        message = build_extraction_message("A title", "A body")
        assert "A title" in message
        assert "A body" in message


class TestProcessArticle:
    """process_article calls Claude, parses JSON, and validates the result."""

    async def test_process_article_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid JSON response is parsed and returned as a dict."""
        client = _make_client(json.dumps(SAMPLE_EXTRACTION))
        _install_client(monkeypatch, client)

        result = await pipeline.process_article(SAMPLE_ARTICLE)

        assert result == SAMPLE_EXTRACTION
        client.messages.create.assert_awaited_once()
        call_kwargs = client.messages.create.await_args.kwargs
        assert call_kwargs["model"] == settings.SONNET_MODEL
        assert call_kwargs["max_tokens"] == 4096
        assert SAMPLE_ARTICLE["title"] in call_kwargs["messages"][0]["content"]

    async def test_process_article_validates_required_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response missing a required key returns None."""
        incomplete = {k: v for k, v in SAMPLE_EXTRACTION.items() if k != "relevance_score"}
        _install_client(monkeypatch, _make_client(json.dumps(incomplete)))

        assert await pipeline.process_article(SAMPLE_ARTICLE) is None

    async def test_process_article_handles_json_with_preamble(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON embedded in surrounding text is extracted and parsed."""
        text = f"Here is the extraction:\n{json.dumps(SAMPLE_EXTRACTION)}\nDone."
        _install_client(monkeypatch, _make_client(text))

        result = await pipeline.process_article(SAMPLE_ARTICLE)

        assert result == SAMPLE_EXTRACTION

    async def test_process_article_handles_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-JSON response returns None instead of raising."""
        _install_client(monkeypatch, _make_client("I could not process this article, sorry."))

        assert await pipeline.process_article(SAMPLE_ARTICLE) is None

    async def test_process_article_retries_on_api_error(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """API errors are retried up to MAX_RETRIES extra attempts."""
        client = _make_failing_client(anthropic.AnthropicError("server overloaded"))
        _install_client(monkeypatch, client)

        await pipeline.process_article(SAMPLE_ARTICLE)

        assert client.messages.create.await_count == settings.MAX_RETRIES + 1

    async def test_process_article_returns_none_after_retries_exhausted(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """A persistently failing API call returns None."""
        client = _make_failing_client(anthropic.AnthropicError("server overloaded"))
        _install_client(monkeypatch, client)

        assert await pipeline.process_article(SAMPLE_ARTICLE) is None

    async def test_process_article_never_logs_api_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_sleep: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Failure logging never includes the API key."""
        _install_client(monkeypatch, _make_failing_client(anthropic.AnthropicError("auth error")))

        with caplog.at_level("DEBUG"):
            await pipeline.process_article(SAMPLE_ARTICLE)

        assert len(caplog.records) > 0
        assert settings.ANTHROPIC_API_KEY not in caplog.text


class TestRunPipeline:
    """run_pipeline orchestrates the batch over the Firestore queue."""

    @pytest.fixture
    def firestore_mocks(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
        """Mock the Firestore helpers on the pipeline module."""
        mocks = {
            "get_unprocessed_articles": AsyncMock(return_value=[]),
            "update_article_after_processing": AsyncMock(),
            "set_article_processing_error": AsyncMock(),
        }
        for name, mock in mocks.items():
            monkeypatch.setattr(pipeline, name, mock)
        return mocks

    def _articles(self, count: int) -> list[dict[str, Any]]:
        return [{**SAMPLE_ARTICLE, "id": f"doc-{i}"} for i in range(count)]

    async def test_run_pipeline_processes_all_articles(
        self, monkeypatch: pytest.MonkeyPatch, firestore_mocks: dict[str, AsyncMock]
    ) -> None:
        """Every article in the batch is processed and written back."""
        firestore_mocks["get_unprocessed_articles"].return_value = self._articles(3)
        monkeypatch.setattr(pipeline, "process_article", AsyncMock(return_value=SAMPLE_EXTRACTION))

        await pipeline.run_pipeline()

        assert firestore_mocks["update_article_after_processing"].await_count == 3
        last_call = firestore_mocks["update_article_after_processing"].await_args
        assert last_call is not None
        (doc_id, updates) = last_call.args
        assert doc_id == "doc-2"
        assert updates["title_en"] == SAMPLE_EXTRACTION["headline"]
        assert updates["relevance_score"] == 8
        assert "summary" not in updates
        assert "body_en" not in updates

    async def test_run_pipeline_handles_mixed_results(
        self, monkeypatch: pytest.MonkeyPatch, firestore_mocks: dict[str, AsyncMock]
    ) -> None:
        """Successes are written back and failures recorded as errors."""
        firestore_mocks["get_unprocessed_articles"].return_value = self._articles(3)
        monkeypatch.setattr(
            pipeline,
            "process_article",
            AsyncMock(side_effect=[SAMPLE_EXTRACTION, None, SAMPLE_EXTRACTION]),
        )

        summary = await pipeline.run_pipeline()

        assert firestore_mocks["update_article_after_processing"].await_count == 2
        firestore_mocks["set_article_processing_error"].assert_awaited_once_with(
            "doc-1", "LLM extraction failed"
        )
        assert summary == {"total": 3, "succeeded": 2, "failed": 1}

    async def test_run_pipeline_returns_summary(
        self, monkeypatch: pytest.MonkeyPatch, firestore_mocks: dict[str, AsyncMock]
    ) -> None:
        """The summary dict reports total, succeeded, and failed counts."""
        firestore_mocks["get_unprocessed_articles"].return_value = self._articles(2)
        monkeypatch.setattr(pipeline, "process_article", AsyncMock(return_value=SAMPLE_EXTRACTION))

        summary = await pipeline.run_pipeline(batch_size=2)

        firestore_mocks["get_unprocessed_articles"].assert_awaited_once_with(limit=2)
        assert summary == {"total": 2, "succeeded": 2, "failed": 0}

    async def test_run_pipeline_empty_queue(self, firestore_mocks: dict[str, AsyncMock]) -> None:
        """An empty queue returns a zeroed summary without any writes."""
        summary = await pipeline.run_pipeline()

        assert summary == {"total": 0, "succeeded": 0, "failed": 0}
        firestore_mocks["update_article_after_processing"].assert_not_awaited()
        firestore_mocks["set_article_processing_error"].assert_not_awaited()

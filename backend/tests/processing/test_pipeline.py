"""Tests for the LLM extraction pipeline.

All external calls are mocked — the Anthropic client is a MagicMock with an
async ``messages.create`` (installed by patching ``anthropic.AsyncAnthropic``,
which the shared ``processing.utils.call_claude`` helper constructs), and
every Firestore helper and Batch API helper is monkeypatched on the pipeline
module. No real API calls, no real database calls, no real sleeps.
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
from processing.prompts import EXTRACTION_PROMPT, TRIAGE_PROMPT, build_extraction_message

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

SAMPLE_TRIAGE = {
    "headline": "BYD Launches New ADAS Feature for Han EV",
    "relevance_score": 2,
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


@pytest.fixture
def firestore_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Mock the Firestore helpers on the pipeline module."""
    mocks = {
        "get_unprocessed_articles": AsyncMock(return_value=[]),
        "update_article_after_processing": AsyncMock(),
        "set_article_processing_error": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(pipeline, name, mock)
    return mocks


@pytest.fixture
def batch_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Mock the Batch API helpers on the pipeline module."""
    mocks = {
        "submit_batch": AsyncMock(return_value="batch_123"),
        "poll_batch": AsyncMock(return_value=MagicMock(processing_status="ended")),
        "get_batch_results": AsyncMock(return_value={}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(pipeline, name, mock)
    return mocks


def _articles(count: int) -> list[dict[str, Any]]:
    return [{**SAMPLE_ARTICLE, "id": f"doc-{i}"} for i in range(count)]


def _all_valid_results(count: int) -> dict[str, str]:
    return {f"doc-{i}": json.dumps(SAMPLE_EXTRACTION) for i in range(count)}


class TestPrompts:
    """The extraction prompt template and message builder."""

    def test_extraction_prompt_is_string(self) -> None:
        """EXTRACTION_PROMPT is a non-empty string."""
        assert isinstance(EXTRACTION_PROMPT, str)
        assert len(EXTRACTION_PROMPT) > 0

    def test_extraction_prompt_requests_json(self) -> None:
        """The prompt instructs Claude to respond in JSON."""
        assert "json" in EXTRACTION_PROMPT.lower()

    def test_build_extraction_message_excludes_prompt(self) -> None:
        """The user message holds only the article; the prompt is sent as system."""
        message = build_extraction_message("A title", "A body")
        assert EXTRACTION_PROMPT not in message

    def test_build_extraction_message_includes_article(self) -> None:
        """The user message contains the article title and body."""
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
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": EXTRACTION_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

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
    """run_pipeline drives the Firestore queue through the Batch API."""

    @pytest.fixture(autouse=True)
    def triage_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """Fail triage open so every article reaches the extraction batch, as before."""
        mock = AsyncMock(return_value=None)
        monkeypatch.setattr(pipeline, "triage_article", mock)
        return mock

    async def test_run_pipeline_processes_all_articles(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """Every article in the batch is processed and written back."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(3)
        batch_mocks["get_batch_results"].return_value = _all_valid_results(3)

        summary = await pipeline.run_pipeline()

        assert summary == {
            "total": 3,
            "succeeded": 3,
            "failed": 0,
            "processing_errors": 0,
            "triage_skipped": 0,
        }
        assert firestore_mocks["update_article_after_processing"].await_count == 3
        last_call = firestore_mocks["update_article_after_processing"].await_args
        assert last_call is not None
        (doc_id, updates) = last_call.args
        assert doc_id == "doc-2"
        assert updates["title_en"] == SAMPLE_EXTRACTION["headline"]
        assert updates["relevance_score"] == 8
        assert "summary" not in updates
        assert "body_en" not in updates

    async def test_run_pipeline_builds_batch_requests(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """Batch requests carry doc IDs, Sonnet, and the cached extraction prompt."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(2)
        batch_mocks["get_batch_results"].return_value = _all_valid_results(2)

        await pipeline.run_pipeline(batch_size=2)

        firestore_mocks["get_unprocessed_articles"].assert_awaited_once_with(limit=2)
        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        requests = submit_call.args[0]
        assert [request["custom_id"] for request in requests] == ["doc-0", "doc-1"]
        params = requests[0]["params"]
        assert params["model"] == settings.SONNET_MODEL
        assert params["max_tokens"] == 4096
        assert SAMPLE_ARTICLE["title"] in params["messages"][0]["content"]
        assert params["system"] == [
            {
                "type": "text",
                "text": EXTRACTION_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_run_pipeline_handles_mixed_results(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """Successes are written back; errored and missing results record errors."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(3)
        batch_mocks["get_batch_results"].return_value = {
            "doc-0": json.dumps(SAMPLE_EXTRACTION),
            "doc-1": None,
        }

        summary = await pipeline.run_pipeline()

        assert firestore_mocks["update_article_after_processing"].await_count == 1
        error_calls = firestore_mocks["set_article_processing_error"].await_args_list
        assert [call.args for call in error_calls] == [
            ("doc-1", "LLM extraction failed"),
            ("doc-2", "LLM extraction failed"),
        ]
        assert summary == {
            "total": 3,
            "succeeded": 1,
            "failed": 2,
            "processing_errors": 2,
            "triage_skipped": 0,
        }

    async def test_run_pipeline_invalid_json_marks_error(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """A result that fails JSON parsing or key validation records an error."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(1)
        batch_mocks["get_batch_results"].return_value = {"doc-0": "not json at all"}

        summary = await pipeline.run_pipeline()

        firestore_mocks["set_article_processing_error"].assert_awaited_once_with(
            "doc-0", "LLM extraction failed"
        )
        assert summary == {
            "total": 1,
            "succeeded": 0,
            "failed": 1,
            "processing_errors": 1,
            "triage_skipped": 0,
        }

    async def test_run_pipeline_empty_queue(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An empty queue skips batch submission and returns a zeroed summary."""
        with caplog.at_level("INFO"):
            summary = await pipeline.run_pipeline()

        assert summary == {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "processing_errors": 0,
            "triage_skipped": 0,
        }
        batch_mocks["submit_batch"].assert_not_awaited()
        firestore_mocks["update_article_after_processing"].assert_not_awaited()
        firestore_mocks["set_article_processing_error"].assert_not_awaited()
        assert "no unprocessed articles" in caplog.text

    async def test_run_pipeline_falls_back_to_sync_on_submission_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed batch submission processes the chunk synchronously instead."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(2)
        batch_mocks["submit_batch"].side_effect = anthropic.AnthropicError("api down")
        process_article_mock = AsyncMock(return_value=SAMPLE_EXTRACTION)
        monkeypatch.setattr(pipeline, "process_article", process_article_mock)

        with caplog.at_level("WARNING"):
            summary = await pipeline.run_pipeline()

        assert summary == {
            "total": 2,
            "succeeded": 2,
            "failed": 0,
            "processing_errors": 0,
            "triage_skipped": 0,
        }
        assert process_article_mock.await_count == 2
        batch_mocks["poll_batch"].assert_not_awaited()
        batch_mocks["get_batch_results"].assert_not_awaited()
        assert "falling back to synchronous processing" in caplog.text

    async def test_run_pipeline_batch_timeout_leaves_articles_queued(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """A poll timeout counts articles as failed without Firestore writes."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(3)
        batch_mocks["poll_batch"].return_value = None

        summary = await pipeline.run_pipeline()

        assert summary == {
            "total": 3,
            "succeeded": 0,
            "failed": 3,
            "processing_errors": 0,
            "triage_skipped": 0,
        }
        firestore_mocks["update_article_after_processing"].assert_not_awaited()
        firestore_mocks["set_article_processing_error"].assert_not_awaited()

    async def test_run_pipeline_chunks_at_100_articles(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """More than 100 articles are submitted as sequential batches."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(150)
        batch_mocks["get_batch_results"].return_value = _all_valid_results(150)

        summary = await pipeline.run_pipeline(batch_size=150)

        assert batch_mocks["submit_batch"].await_count == 2
        submit_calls = batch_mocks["submit_batch"].await_args_list
        assert len(submit_calls[0].args[0]) == 100
        assert len(submit_calls[1].args[0]) == 50
        assert summary == {
            "total": 150,
            "succeeded": 150,
            "failed": 0,
            "processing_errors": 0,
            "triage_skipped": 0,
        }

    async def test_run_pipeline_article_missing_body_gets_error(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
    ) -> None:
        """An article missing a body is excluded from the batch but still errored."""
        incomplete = {k: v for k, v in SAMPLE_ARTICLE.items() if k != "body"}
        incomplete["id"] = "doc-1"
        articles = [{**SAMPLE_ARTICLE, "id": "doc-0"}, incomplete]
        firestore_mocks["get_unprocessed_articles"].return_value = articles
        batch_mocks["get_batch_results"].return_value = _all_valid_results(1)

        summary = await pipeline.run_pipeline()

        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        assert [request["custom_id"] for request in submit_call.args[0]] == ["doc-0"]
        firestore_mocks["set_article_processing_error"].assert_awaited_once_with(
            "doc-1", "LLM extraction failed"
        )
        assert summary == {
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "processing_errors": 1,
            "triage_skipped": 0,
        }

    async def test_run_pipeline_logs_summary(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The run ends with a summary log of all four counts."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(2)
        batch_mocks["get_batch_results"].return_value = _all_valid_results(2)

        with caplog.at_level("INFO"):
            await pipeline.run_pipeline()

        assert (
            "pipeline summary: total=2 succeeded=2 failed=0 processing_errors=0 triage_skipped=0"
            in caplog.text
        )


class TestTriageArticle:
    """triage_article calls Haiku, parses JSON, and validates the result."""

    async def test_triage_article_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid triage response is parsed and returned as a dict."""
        triage = {**SAMPLE_TRIAGE, "relevance_score": 8}
        client = _make_client(json.dumps(triage))
        _install_client(monkeypatch, client)

        result = await pipeline.triage_article(SAMPLE_ARTICLE)

        assert result == triage
        client.messages.create.assert_awaited_once()
        call_kwargs = client.messages.create.await_args.kwargs
        assert call_kwargs["model"] == settings.HAIKU_MODEL
        assert call_kwargs["max_tokens"] == 256
        assert SAMPLE_ARTICLE["title"] in call_kwargs["messages"][0]["content"]
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": TRIAGE_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_triage_article_api_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """A persistently failing API call fails open with None."""
        client = _make_failing_client(anthropic.AnthropicError("server overloaded"))
        _install_client(monkeypatch, client)

        assert await pipeline.triage_article(SAMPLE_ARTICLE) is None

    async def test_triage_article_invalid_json_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-JSON response fails open with None."""
        _install_client(monkeypatch, _make_client("I could not triage this article, sorry."))

        assert await pipeline.triage_article(SAMPLE_ARTICLE) is None

    async def test_triage_article_missing_key_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response missing a required key fails open with None."""
        incomplete = {k: v for k, v in SAMPLE_TRIAGE.items() if k != "content_type"}
        _install_client(monkeypatch, _make_client(json.dumps(incomplete)))

        assert await pipeline.triage_article(SAMPLE_ARTICLE) is None

    @pytest.mark.parametrize("bad_score", ["high", 7.5, True, None])
    async def test_triage_article_non_int_score_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, bad_score: Any
    ) -> None:
        """A non-integer relevance_score (including bool) fails open with None."""
        _install_client(
            monkeypatch, _make_client(json.dumps({**SAMPLE_TRIAGE, "relevance_score": bad_score}))
        )

        assert await pipeline.triage_article(SAMPLE_ARTICLE) is None

    async def test_triage_article_missing_body_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An article without a body fails open with None and never calls the API."""
        client = _make_client(json.dumps(SAMPLE_TRIAGE))
        _install_client(monkeypatch, client)
        incomplete = {k: v for k, v in SAMPLE_ARTICLE.items() if k != "body"}

        assert await pipeline.triage_article(incomplete) is None
        client.messages.create.assert_not_awaited()


class TestRunPipelineTriage:
    """run_pipeline filters low-relevance articles via real triage logic."""

    @pytest.fixture
    def triage_claude(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """Mock call_claude on the pipeline module (only triage uses it on the batch path)."""
        mock = AsyncMock(return_value=json.dumps({**SAMPLE_TRIAGE, "relevance_score": 8}))
        monkeypatch.setattr(pipeline, "call_claude", mock)
        return mock

    def _scores(self, *scores: int) -> list[str]:
        return [json.dumps({**SAMPLE_TRIAGE, "relevance_score": s}) for s in scores]

    async def test_triage_filters_low_relevance_articles(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
    ) -> None:
        """Below-threshold articles are written as triage-only; the rest are batched."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(3)
        triage_claude.side_effect = self._scores(2, 8, 3)
        batch_mocks["get_batch_results"].return_value = {"doc-1": json.dumps(SAMPLE_EXTRACTION)}

        summary = await pipeline.run_pipeline()

        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        assert [request["custom_id"] for request in submit_call.args[0]] == ["doc-1"]
        update_calls = firestore_mocks["update_article_after_processing"].await_args_list
        assert update_calls[0].args == (
            "doc-0",
            {
                "title_en": SAMPLE_TRIAGE["headline"],
                "relevance_score": 2,
                "content_type": "news",
                "triage_only": True,
            },
        )
        assert summary == {
            "total": 3,
            "succeeded": 3,
            "failed": 0,
            "processing_errors": 0,
            "triage_skipped": 2,
        }

    async def test_triage_all_below_threshold_skips_extraction(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
    ) -> None:
        """When every article scores below threshold, no batch is ever submitted."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(3)
        triage_claude.side_effect = self._scores(1, 2, 3)

        summary = await pipeline.run_pipeline()

        batch_mocks["submit_batch"].assert_not_awaited()
        update_calls = firestore_mocks["update_article_after_processing"].await_args_list
        assert len(update_calls) == 3
        assert all(call.args[1]["triage_only"] is True for call in update_calls)
        assert summary == {
            "total": 3,
            "succeeded": 3,
            "failed": 0,
            "processing_errors": 0,
            "triage_skipped": 3,
        }

    async def test_triage_all_above_threshold_batches_everything(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
    ) -> None:
        """When every article passes triage, all reach the batch with no triage writes."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(2)
        triage_claude.side_effect = self._scores(8, 9)
        batch_mocks["get_batch_results"].return_value = _all_valid_results(2)

        summary = await pipeline.run_pipeline()

        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        assert [request["custom_id"] for request in submit_call.args[0]] == ["doc-0", "doc-1"]
        update_calls = firestore_mocks["update_article_after_processing"].await_args_list
        assert all("triage_only" not in call.args[1] for call in update_calls)
        assert summary["triage_skipped"] == 0

    async def test_triage_score_at_threshold_passes(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
    ) -> None:
        """A score exactly at RELEVANCE_THRESHOLD proceeds to full extraction."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(1)
        triage_claude.side_effect = self._scores(settings.RELEVANCE_THRESHOLD)
        batch_mocks["get_batch_results"].return_value = _all_valid_results(1)

        summary = await pipeline.run_pipeline()

        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        assert [request["custom_id"] for request in submit_call.args[0]] == ["doc-0"]
        assert summary["triage_skipped"] == 0

    async def test_triage_api_failure_fails_open(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
    ) -> None:
        """A Haiku failure (call_claude None) sends every article to extraction."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(2)
        triage_claude.return_value = None
        triage_claude.side_effect = None
        batch_mocks["get_batch_results"].return_value = _all_valid_results(2)

        summary = await pipeline.run_pipeline()

        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        assert [request["custom_id"] for request in submit_call.args[0]] == ["doc-0", "doc-1"]
        assert summary["triage_skipped"] == 0

    async def test_triage_malformed_response_fails_open(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
    ) -> None:
        """A malformed triage response sends the article to extraction, no triage write."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(1)
        triage_claude.return_value = "not json at all"
        batch_mocks["get_batch_results"].return_value = _all_valid_results(1)

        summary = await pipeline.run_pipeline()

        submit_call = batch_mocks["submit_batch"].await_args
        assert submit_call is not None
        assert [request["custom_id"] for request in submit_call.args[0]] == ["doc-0"]
        update_calls = firestore_mocks["update_article_after_processing"].await_args_list
        assert all("triage_only" not in call.args[1] for call in update_calls)
        assert summary["triage_skipped"] == 0

    async def test_triage_logs_summary(
        self,
        firestore_mocks: dict[str, AsyncMock],
        batch_mocks: dict[str, AsyncMock],
        triage_claude: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Triage ends with a summary log of total, passed, and skipped counts."""
        firestore_mocks["get_unprocessed_articles"].return_value = _articles(3)
        triage_claude.side_effect = self._scores(2, 8, 3)
        batch_mocks["get_batch_results"].return_value = {"doc-1": json.dumps(SAMPLE_EXTRACTION)}

        with caplog.at_level("INFO"):
            await pipeline.run_pipeline()

        assert "Triage: 3 articles, 1 passed threshold, 2 skipped" in caplog.text

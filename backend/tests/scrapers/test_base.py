"""Tests for scrapers.base.BaseScraper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import MAX_RETRIES, SCRAPE_DELAY_MAX, SCRAPE_DELAY_MIN
from scrapers.base import BaseScraper

if TYPE_CHECKING:
    from collections.abc import Iterator

ARTICLE_URL = "https://example.com/article"


class FakeBaseScraper(BaseScraper):
    """Concrete BaseScraper with stub implementations for testing."""

    SOURCE_NAME = "fake"
    BASE_URL = "https://example.com"

    async def discover_articles(self) -> list[dict[str, str]]:
        """Return no articles."""
        return []

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Return an empty article."""
        return {}


@pytest.fixture
def mock_sleep() -> Iterator[AsyncMock]:
    """Replace asyncio.sleep so tests never actually wait."""
    with patch("scrapers.base.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        yield sleep_mock


@pytest.fixture
def scraper() -> FakeBaseScraper:
    """Fresh scraper instance."""
    return FakeBaseScraper()


async def test_delay_sleeps_within_range(scraper: FakeBaseScraper, mock_sleep: AsyncMock) -> None:
    await scraper._delay()
    mock_sleep.assert_awaited_once()
    assert mock_sleep.await_args is not None
    delay = mock_sleep.await_args.args[0]
    assert SCRAPE_DELAY_MIN <= delay <= SCRAPE_DELAY_MAX


@pytest.mark.usefixtures("mock_sleep")
def test_get_random_ua_returns_string(scraper: FakeBaseScraper) -> None:
    ua = scraper._get_random_ua()
    assert isinstance(ua, str)
    assert ua in BaseScraper._USER_AGENTS


@pytest.mark.usefixtures("mock_sleep")
def test_get_random_ua_varies(scraper: FakeBaseScraper) -> None:
    results = {scraper._get_random_ua() for _ in range(20)}
    assert len(results) > 1


async def test_request_with_retry_success_first_try(
    scraper: FakeBaseScraper, mock_sleep: AsyncMock
) -> None:
    fetch = AsyncMock(return_value="<html>ok</html>")
    result = await scraper._request_with_retry(fetch, ARTICLE_URL)
    assert result == "<html>ok</html>"
    assert scraper.requests_made == 1
    assert scraper.error_count == 0
    fetch.assert_awaited_once_with(ARTICLE_URL)


async def test_request_with_retry_success_after_retries(
    scraper: FakeBaseScraper, mock_sleep: AsyncMock
) -> None:
    fetch = AsyncMock(side_effect=[ConnectionError("refused"), TimeoutError("slow"), "recovered"])
    result = await scraper._request_with_retry(fetch, ARTICLE_URL)
    assert result == "recovered"
    assert fetch.await_count == 3
    assert scraper.requests_made == 1
    assert scraper.error_count == 2


async def test_request_with_retry_all_retries_exhausted(
    scraper: FakeBaseScraper, mock_sleep: AsyncMock
) -> None:
    fetch = AsyncMock(side_effect=ValueError("bad response"))
    result = await scraper._request_with_retry(fetch, ARTICLE_URL)
    assert result is None
    assert fetch.await_count == MAX_RETRIES
    assert scraper.requests_made == 0
    assert scraper.error_count == MAX_RETRIES
    assert len(scraper.errors) == MAX_RETRIES
    first = scraper.errors[0]
    assert first["url"] == ARTICLE_URL
    assert first["status_code"] is None
    assert "ValueError" in first["message"]


async def test_request_with_retry_logs_every_request(
    scraper: FakeBaseScraper, mock_sleep: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    fetch = AsyncMock(side_effect=[ConnectionError("refused"), "ok"])
    with caplog.at_level(logging.INFO, logger="scrapers.base"):
        await scraper._request_with_retry(fetch, ARTICLE_URL)
    per_attempt = [r for r in caplog.records if ARTICLE_URL in r.getMessage()]
    assert len(per_attempt) == 2  # one log line per attempt: 1 failure + 1 success
    assert per_attempt[0].levelno == logging.WARNING
    assert per_attempt[1].levelno == logging.INFO


@pytest.mark.usefixtures("mock_sleep")
def test_health_metrics_shape(scraper: FakeBaseScraper) -> None:
    metrics = scraper._get_health_metrics()
    expected_keys = {"source_name", "requests_made", "error_count", "errors", "duration_seconds"}
    assert set(metrics) == expected_keys
    assert metrics["source_name"] == "fake"
    assert isinstance(metrics["requests_made"], int)
    assert isinstance(metrics["error_count"], int)
    assert isinstance(metrics["errors"], list)
    assert isinstance(metrics["duration_seconds"], float)


@pytest.mark.usefixtures("mock_sleep")
def test_reset_health_metrics(scraper: FakeBaseScraper) -> None:
    scraper.requests_made = 5
    scraper.error_count = 2
    scraper.errors.append({"url": ARTICLE_URL, "status_code": 500, "message": "boom"})
    old_start = scraper.start_time
    scraper.reset_health_metrics()
    assert scraper.requests_made == 0
    assert scraper.error_count == 0
    assert scraper.errors == []
    assert scraper.start_time >= old_start


@pytest.mark.parametrize(
    "raised",
    [ValueError("bad json"), ConnectionError("refused"), TimeoutError("timed out")],
)
async def test_never_crashes_on_exception(
    scraper: FakeBaseScraper, mock_sleep: AsyncMock, raised: Exception
) -> None:
    fetch = AsyncMock(side_effect=raised)
    result = await scraper._request_with_retry(fetch, ARTICLE_URL)
    assert result is None
    assert scraper.error_count == MAX_RETRIES
    assert type(raised).__name__ in scraper.errors[0]["message"]

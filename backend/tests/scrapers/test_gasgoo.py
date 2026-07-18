"""Tests for scrapers.sources.gasgoo.GasgooScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scrapers.sources.gasgoo import GasgooScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "gasgoo"

ARTICLE_URL = (
    "https://autonews.gasgoo.com/articles/news/"
    "placeholder-automaker-launches-placeholder-sedan-with-city-noa-2078000000000000001"
)


def _fixture(name: str) -> str:
    """Read a fixture file from tests/fixtures/gasgoo/."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _mock_response(text: str) -> MagicMock:
    """Build a fake httpx.Response carrying the given body text."""
    response = MagicMock(spec=httpx.Response)
    response.text = text
    return response


@pytest.fixture
def mock_sleep() -> Iterator[AsyncMock]:
    """Replace asyncio.sleep so tests never actually wait."""
    with patch("scrapers.base.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        yield sleep_mock


@pytest.fixture
def mock_client() -> AsyncMock:
    """Mocked httpx client; no real HTTP requests."""
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
async def scraper(mock_client: AsyncMock) -> AsyncIterator[GasgooScraper]:
    """GasgooScraper whose context manager yields the mocked client."""
    with patch("scrapers.static.httpx.AsyncClient", return_value=mock_client):
        async with GasgooScraper() as instance:
            yield instance


def test_source_name() -> None:
    assert GasgooScraper.SOURCE_NAME == "gasgoo"


def test_base_url() -> None:
    assert GasgooScraper.BASE_URL == "https://autonews.gasgoo.com"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("feed.xml"))
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    assert len(result) == 4
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
        assert entry["publish_date"].startswith("2026-07-")


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("feed.xml"))
    result = await scraper.discover_articles()
    assert result
    for entry in result:
        assert entry["url"].startswith("https://autonews.gasgoo.com/articles/")
    # One feed fetch per category, deduped to unique article URLs.
    assert mock_client.get.await_count == len(GasgooScraper._FEED_CLASS_IDS)
    urls = [entry["url"] for entry in result]
    assert len(urls) == len(set(urls))


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    expected_keys = {
        "source_name",
        "source_url",
        "title",
        "body",
        "publish_date",
        "scrape_date",
        "language",
        "raw_html",
    }
    assert set(result.keys()) == expected_keys
    for key in expected_keys:
        assert result[key], f"field {key} is empty"
    assert result["source_name"] == "gasgoo"
    assert result["source_url"] == ARTICLE_URL
    assert result["title"] == "Placeholder Automaker Launches Placeholder Sedan with City NOA"
    assert result["publish_date"] == "2026-07-18T13:56:16+00:00"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_is_clean(scraper: GasgooScraper, mock_client: AsyncMock) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    body = result["body"]
    assert "Placeholder Automaker today launched the Placeholder Sedan" in body
    # Site chrome from the fixture must not leak into the body.
    assert "Subscribe to our newsletter" not in body
    assert "Home / EV /" not in body
    assert "By Placeholder Reporter" not in body
    assert "Most Read" not in body
    assert "Sidebar placeholder story" not in body
    assert "All Rights Reserved" not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_en(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["language"] == "en"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: GasgooScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article_minimal.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["title"] == "Minimal Placeholder Headline"
    assert "One short placeholder paragraph" in result["body"]
    assert result["publish_date"] == ""
    assert result["language"] == "en"

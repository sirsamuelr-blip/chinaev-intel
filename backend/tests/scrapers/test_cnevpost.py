"""Tests for scrapers.sources.cnevpost.CnEVPostScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scrapers.sources.cnevpost import CnEVPostScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "cnevpost"

ARTICLE_URL = "https://cnevpost.com/2026/07/18/placeholder-ev-maker-day-2026-faketown/"


def _fixture(name: str) -> str:
    """Read a fixture file from tests/fixtures/cnevpost/."""
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
async def scraper(mock_client: AsyncMock) -> AsyncIterator[CnEVPostScraper]:
    """CnEVPostScraper whose context manager yields the mocked client."""
    with patch("scrapers.static.httpx.AsyncClient", return_value=mock_client):
        async with CnEVPostScraper() as instance:
            yield instance


def test_source_name() -> None:
    assert CnEVPostScraper.SOURCE_NAME == "cnevpost"


def test_base_url() -> None:
    assert CnEVPostScraper.BASE_URL == "https://cnevpost.com"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("feed.xml"))
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    assert len(result) == 4
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
        assert entry["publish_date"].startswith("2026-07-18T")


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("feed.xml"))
    result = await scraper.discover_articles()
    assert result
    for entry in result:
        assert entry["url"].startswith("https://cnevpost.com/2026/")
    # A single feed fetch covers all categories.
    assert mock_client.get.await_count == 1


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: CnEVPostScraper, mock_client: AsyncMock
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
    assert result["source_name"] == "cnevpost"
    assert result["source_url"] == ARTICLE_URL
    assert result["title"] == "Placeholder EV Maker Day 2026 to be held in Faketown after user vote"
    assert result["publish_date"] == "2026-07-18T14:40:02+00:00"
    # raw_html keeps the original page even though the body strips chrome.
    assert "subscription-container" in result["raw_html"]


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_is_clean(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    body = result["body"]
    assert "Placeholder EV Maker will hold its most important annual event" in body
    # Chrome embedded inside entry-content must be stripped.
    assert "Join us on" not in body
    assert "Google News" not in body
    assert "Related placeholder story" not in body
    # Chrome outside entry-content must not leak in.
    assert "Sign up for our free newsletter" not in body
    assert "Trending Now" not in body
    assert "Sidebar placeholder story" not in body
    assert "All Rights Reserved" not in body
    assert "Placeholder Reporter" not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_en(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["language"] == "en"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: CnEVPostScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article_minimal.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["title"] == "Minimal Placeholder Headline"
    assert "One short placeholder paragraph" in result["body"]
    assert result["publish_date"] == ""
    assert result["language"] == "en"

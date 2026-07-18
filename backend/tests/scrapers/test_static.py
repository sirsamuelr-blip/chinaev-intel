"""Tests for scrapers.static.StaticScraper."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from config.settings import MAX_RETRIES
from scrapers.static import StaticScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

ARTICLE_URL = "https://fake.example.com/article"
FEED_URL = "https://fake.example.com/feed"

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Article One</title>
      <link>https://example.com/article-1</link>
      <pubDate>Mon, 14 Jul 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Article Two</title>
      <link>https://example.com/article-2</link>
      <pubDate>Tue, 15 Jul 2026 10:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

EMPTY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Empty Feed</title>
  </channel>
</rss>"""

NO_DATE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Undated Article</title>
      <link>https://example.com/undated</link>
    </item>
  </channel>
</rss>"""


class FakeStaticScraper(StaticScraper):
    """Concrete StaticScraper with stub implementations for testing."""

    SOURCE_NAME = "fake"
    BASE_URL = "https://fake.example.com"

    async def discover_articles(self) -> list[dict[str, str]]:
        """Return no articles."""
        return []

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Return an empty article."""
        return {}


def _mock_response(text: str = "<html></html>") -> MagicMock:
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
async def scraper(mock_client: AsyncMock) -> AsyncIterator[FakeStaticScraper]:
    """Scraper whose context manager yields the mocked client."""
    with patch("scrapers.static.httpx.AsyncClient", return_value=mock_client):
        async with FakeStaticScraper() as instance:
            yield instance


async def test_context_manager_creates_client() -> None:
    async with FakeStaticScraper() as instance:
        assert isinstance(instance._client, httpx.AsyncClient)


async def test_context_manager_closes_client(mock_client: AsyncMock) -> None:
    with patch("scrapers.static.httpx.AsyncClient", return_value=mock_client):
        async with FakeStaticScraper():
            pass
    mock_client.aclose.assert_awaited_once()


async def test_fetch_page_outside_context_raises() -> None:
    instance = FakeStaticScraper()
    with pytest.raises(RuntimeError, match="async context manager"):
        await instance.fetch_page(ARTICLE_URL)


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_success(scraper: FakeStaticScraper, mock_client: AsyncMock) -> None:
    mock_client.get.return_value = _mock_response("<html>hello</html>")
    result = await scraper.fetch_page(ARTICLE_URL)
    assert result == "<html>hello</html>"
    assert scraper.requests_made == 1
    assert scraper.error_count == 0


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_sets_random_ua(
    scraper: FakeStaticScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response()
    await scraper.fetch_page(ARTICLE_URL)
    mock_client.get.assert_awaited_once()
    assert mock_client.get.await_args is not None
    assert mock_client.get.await_args.args == (ARTICLE_URL,)
    headers = mock_client.get.await_args.kwargs["headers"]
    assert headers["User-Agent"] in FakeStaticScraper._USER_AGENTS


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_retries_on_failure(
    scraper: FakeStaticScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = [httpx.ConnectError("refused"), _mock_response("<html>ok</html>")]
    result = await scraper.fetch_page(ARTICLE_URL)
    assert result == "<html>ok</html>"
    assert mock_client.get.await_count == 2
    assert scraper.error_count == 1


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_returns_none_after_exhausted_retries(
    scraper: FakeStaticScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.fetch_page(ARTICLE_URL)
    assert result is None
    assert mock_client.get.await_count == MAX_RETRIES
    assert scraper.error_count == MAX_RETRIES


def test_parse_html_returns_soup() -> None:
    instance = FakeStaticScraper()
    soup = instance.parse_html("<html><body><h1>Title</h1></body></html>")
    assert isinstance(soup, BeautifulSoup)
    heading = soup.find("h1")
    assert heading is not None
    assert heading.get_text() == "Title"


def test_parse_html_handles_malformed_html() -> None:
    instance = FakeStaticScraper()
    soup = instance.parse_html("<div><p>Unclosed <b>tags")
    assert isinstance(soup, BeautifulSoup)
    assert soup.get_text().strip() == "Unclosed tags"


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_and_parse_success(scraper: FakeStaticScraper, mock_client: AsyncMock) -> None:
    mock_client.get.return_value = _mock_response("<html><body><h1>Title</h1></body></html>")
    soup = await scraper.fetch_and_parse(ARTICLE_URL)
    assert isinstance(soup, BeautifulSoup)
    assert soup.find("h1") is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_and_parse_returns_none_on_failure(
    scraper: FakeStaticScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.fetch_and_parse(ARTICLE_URL)
    assert result is None


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_feed_success(scraper: FakeStaticScraper, mock_client: AsyncMock) -> None:
    mock_client.get.return_value = _mock_response(SAMPLE_RSS)
    result = await scraper.fetch_feed(FEED_URL)
    assert result == [
        {
            "url": "https://example.com/article-1",
            "title": "Article One",
            "publish_date": "2026-07-14T08:00:00+00:00",
        },
        {
            "url": "https://example.com/article-2",
            "title": "Article Two",
            "publish_date": "2026-07-15T10:30:00+00:00",
        },
    ]


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_feed_returns_none_on_fetch_failure(
    scraper: FakeStaticScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.fetch_feed(FEED_URL)
    assert result is None


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_feed_empty_feed(scraper: FakeStaticScraper, mock_client: AsyncMock) -> None:
    mock_client.get.return_value = _mock_response(EMPTY_RSS)
    result = await scraper.fetch_feed(FEED_URL)
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_feed_missing_publish_date(
    scraper: FakeStaticScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(NO_DATE_RSS)
    result = await scraper.fetch_feed(FEED_URL)
    assert result == [
        {
            "url": "https://example.com/undated",
            "title": "Undated Article",
            "publish_date": "",
        }
    ]

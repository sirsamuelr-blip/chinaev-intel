"""Tests for scrapers.sources.baidu_news.BaiduNewsScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scrapers.sources.baidu_news import BaiduNewsScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "baidu_news"

ARTICLE_URL = "https://www.example-auto.cn/news/placeholder-adas-launch.html"

# Unique article links in search_results.html: one legacy result plus six
# news-normal results, one of which has no date span.
FIXTURE_RESULT_COUNT = 7


def _fixture(name: str) -> str:
    """Read a fixture file from tests/fixtures/baidu_news/."""
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
async def scraper(mock_client: AsyncMock) -> AsyncIterator[BaiduNewsScraper]:
    """BaiduNewsScraper whose context manager yields the mocked client."""
    with patch("scrapers.static.httpx.AsyncClient", return_value=mock_client):
        async with BaiduNewsScraper() as instance:
            yield instance


def test_source_name() -> None:
    assert BaiduNewsScraper.SOURCE_NAME == "baidu_news"


def test_base_url() -> None:
    assert BaiduNewsScraper.BASE_URL == "https://news.baidu.com"


def test_search_keywords_defined() -> None:
    assert isinstance(BaiduNewsScraper.SEARCH_KEYWORDS, list)
    assert BaiduNewsScraper.SEARCH_KEYWORDS
    for keyword in BaiduNewsScraper.SEARCH_KEYWORDS:
        assert isinstance(keyword, str)
        assert keyword


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("search_results.html"))
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    assert len(result) == FIXTURE_RESULT_COUNT
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
    # One search request per keyword.
    assert mock_client.get.await_count == len(BaiduNewsScraper.SEARCH_KEYWORDS)


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("search_results.html"))
    result = await scraper.discover_articles()
    assert result
    urls = {entry["url"] for entry in result}
    for url in urls:
        assert url.startswith(("http://", "https://"))
    # Baidu-property and external-site links are both kept as-is.
    assert "https://baijiahao.baidu.com/s?id=1000000000000000001&wfr=spider&for=pc" in urls
    assert ARTICLE_URL in urls
    # Absolute Beijing-time listing dates are normalized to UTC.
    by_url = {entry["url"]: entry for entry in result}
    legacy = by_url["https://baike.baidu.com/item/placeholder-encyclopedia-entry"]
    assert legacy["publish_date"] == "2026-07-09T06:34:00+00:00"
    # A result without a date span degrades to an empty string.
    assert by_url["https://ev.example-media.cn/green/"]["publish_date"] == ""


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_deduplicates(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    # Two keyword searches return the same results page, so every URL
    # from the second search is a duplicate of the first.
    mock_client.get.return_value = _mock_response(_fixture("search_results.html"))
    with patch.object(BaiduNewsScraper, "SEARCH_KEYWORDS", ["新能源汽车", "智能驾驶"]):
        result = await scraper.discover_articles()
    assert mock_client.get.await_count == 2
    urls = [entry["url"] for entry in result]
    assert len(urls) == len(set(urls))
    assert len(result) == FIXTURE_RESULT_COUNT


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
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
    assert result["source_name"] == "baidu_news"
    assert result["source_url"] == ARTICLE_URL
    assert result["title"] == "某新势力品牌发布全新一代智能驾驶系统"
    # article:published_time is +08:00 Beijing time, normalized to UTC.
    assert result["publish_date"] == "2026-07-17T02:23:00+00:00"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_has_content(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    body = result["body"]
    assert len(body) > 100
    assert "占位正文第一段" in body
    assert "占位正文第三段" in body
    # Site chrome outside the article container must not leak in.
    assert "首页" not in body
    assert "版权所有" not in body
    assert "placeholderAnalytics" not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_zh(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["language"] == "zh"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: BaiduNewsScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article_minimal.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    # No h1 and no article container: title falls back to <title>, body
    # to the page's paragraph text, and the missing date degrades to "".
    assert result["title"] == "极简占位页面标题"
    assert "只有一段简短的占位正文" in result["body"]
    assert result["publish_date"] == ""
    assert result["language"] == "zh"

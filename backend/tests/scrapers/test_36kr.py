"""Tests for scrapers.sources._36kr.ThirtySixKrScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scrapers.sources._36kr import ThirtySixKrScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "36kr"

ARTICLE_URL = "https://36kr.com/p/3900000000000001"


def _fixture(name: str) -> str:
    """Read a fixture file from tests/fixtures/36kr/."""
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
async def scraper(mock_client: AsyncMock) -> AsyncIterator[ThirtySixKrScraper]:
    """ThirtySixKrScraper whose context manager yields the mocked client."""
    with patch("scrapers.static.httpx.AsyncClient", return_value=mock_client):
        async with ThirtySixKrScraper() as instance:
            yield instance


def test_source_name() -> None:
    assert ThirtySixKrScraper.SOURCE_NAME == "36kr"


def test_base_url() -> None:
    assert ThirtySixKrScraper.BASE_URL == "https://36kr.com"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("listing.html"))
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    # The fixture holds 4 article items plus 1 video item, which is skipped.
    assert len(result) == 4
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
        assert entry["publish_date"].startswith("2026-07-")


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("listing.html"))
    result = await scraper.discover_articles()
    assert result
    for entry in result:
        assert entry["url"].startswith("https://36kr.com/p/")
    # A single listing page fetch covers the whole channel.
    assert mock_client.get.await_count == 1
    urls = [entry["url"] for entry in result]
    assert len(urls) == len(set(urls))


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_falls_back_to_dom_cards(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    # Break the JSON assignment so discovery must use the rendered cards.
    html = _fixture("listing.html").replace("window.initialState=", "window.initialStateGone=")
    mock_client.get.return_value = _mock_response(html)
    result = await scraper.discover_articles()
    assert len(result) == 4
    for entry in result:
        assert entry["url"].startswith("https://36kr.com/p/")
        assert entry["title"]
        assert entry["publish_date"] == ""


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
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
    assert result["source_name"] == "36kr"
    assert result["source_url"] == ARTICLE_URL
    assert result["title"] == "占位车企发布全新城市NOA智能驾驶系统"
    # publishTime 1784543400000 ms = 2026-07-20T18:30 Beijing = 10:30 UTC.
    assert result["publish_date"] == "2026-07-20T10:30:00+00:00"
    # raw_html keeps the original page, including the embedded JSON.
    assert "window.initialState=" in result["raw_html"]


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_is_clean(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    body = result["body"]
    assert "占位车企今日正式发布全新一代城市NOA智能驾驶系统" in body
    # Page chrome must not leak into the body.
    assert "36氪首页" not in body
    assert "下载36氪APP" not in body
    assert "相关推荐" not in body
    assert "评论区占位" not in body
    assert "热门文章" not in body
    assert "京ICP备" not in body
    # The article header (author/date line) sits outside the body hook.
    assert "2026年07月20日 18:30" not in body
    # No script or style text either.
    assert "placeholderBundle" not in body
    assert "line-height" not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_zh(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["language"] == "zh"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.side_effect = httpx.ConnectError("refused")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: ThirtySixKrScraper, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(_fixture("article_minimal.html"))
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["title"] == "极简占位标题"
    assert "一段极简占位正文" in result["body"]
    assert result["publish_date"] == ""
    assert result["language"] == "zh"

"""Tests for scrapers.sources.autohome.AutohomeScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scrapers.sources.autohome import AutohomeScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "autohome"

ARTICLE_URL = "https://www.autohome.com.cn/news/202607/9000001.html"

# listing.html holds seven li[data-artidanchor] items, one of which
# duplicates the first item's URL, plus an ad-slot li and sidebar links
# that must be skipped.
FIXTURE_UNIQUE_COUNT = 6

ARTICLE_KEYS = {
    "source_name",
    "source_url",
    "title",
    "body",
    "publish_date",
    "scrape_date",
    "language",
    "raw_html",
}


def _fixture(name: str) -> str:
    """Read a fixture file from tests/fixtures/autohome/."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def mock_sleep() -> Iterator[AsyncMock]:
    """Replace asyncio.sleep so tests never actually wait."""
    with patch("scrapers.base.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        yield sleep_mock


@pytest.fixture
def mock_page() -> AsyncMock:
    """Mocked Playwright page; page.content() is set per test."""
    return AsyncMock()


@pytest.fixture
def mock_browser_context(mock_page: AsyncMock) -> AsyncMock:
    """Mocked Playwright browser context that opens the mocked page."""
    context = AsyncMock()
    context.new_page.return_value = mock_page
    return context


@pytest.fixture
def mock_browser(mock_browser_context: AsyncMock) -> AsyncMock:
    """Mocked Playwright browser that creates the mocked context."""
    browser = AsyncMock()
    browser.new_context.return_value = mock_browser_context
    return browser


@pytest.fixture
def mock_playwright(mock_browser: AsyncMock) -> AsyncMock:
    """Mocked Playwright driver that launches the mocked browser."""
    playwright = AsyncMock()
    playwright.chromium.launch.return_value = mock_browser
    return playwright


@pytest.fixture
def mock_playwright_start(mock_playwright: AsyncMock) -> Iterator[MagicMock]:
    """Patch async_playwright() so no real browser is ever launched."""
    manager = MagicMock()
    manager.start = AsyncMock(return_value=mock_playwright)
    with patch("scrapers.dynamic.async_playwright", return_value=manager):
        yield manager


@pytest.fixture
async def scraper(mock_playwright_start: MagicMock) -> AsyncIterator[AutohomeScraper]:
    """AutohomeScraper entered with the fully mocked Playwright chain."""
    async with AutohomeScraper() as instance:
        yield instance


def test_source_name() -> None:
    assert AutohomeScraper.SOURCE_NAME == "autohome"


def test_base_url() -> None:
    assert AutohomeScraper.BASE_URL == "https://www.autohome.com.cn"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("listing.html")
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    assert len(result) == FIXTURE_UNIQUE_COUNT
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
    # One listing fetch per section (news, tech, ev).
    assert mock_page.goto.await_count == len(AutohomeScraper._LISTING_PATHS)


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("listing.html")
    result = await scraper.discover_articles()
    assert result
    urls = [entry["url"] for entry in result]
    for url in urls:
        # Protocol-relative hrefs are made absolute; tracking fragments stripped.
        assert url.startswith("https://")
        assert "#" not in url
    # Both article URL shapes survive normalization.
    assert ARTICLE_URL in urls
    assert "https://www.autohome.com.cn/article?id=PlaceHolderTok=" in urls
    # The duplicate listing entry was deduplicated by URL.
    assert len(urls) == len(set(urls))
    by_url = {entry["url"]: entry for entry in result}
    # Absolute Beijing-time listing dates are normalized to UTC.
    absolute = by_url["https://www.autohome.com.cn/tech/202607/9000003.html"]
    assert absolute["publish_date"] == "2026-05-15T10:00:00+00:00"
    # Relative dates parse to valid ISO timestamps.
    relative = by_url[ARTICLE_URL]
    assert datetime.fromisoformat(relative["publish_date"])
    # An item without a date span degrades to an empty string.
    assert by_url["https://www.autohome.com.cn/news/202607/9000005.html"]["publish_date"] == ""


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("networkidle timeout")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert set(result.keys()) == ARTICLE_KEYS
    for key in ARTICLE_KEYS:
        assert result[key]
    assert result["source_name"] == "autohome"
    assert result["source_url"] == ARTICLE_URL
    # Title and date come from the __NEXT_DATA__ payload.
    assert result["title"] == "占位文章标题：某品牌智能驾驶系统发布"
    # publishDate 2026-07-18 20:13:23 Beijing time == 12:13:23 UTC.
    assert result["publish_date"] == "2026-07-18T12:13:23+00:00"
    assert "占位正文第一段" in result["body"]
    assert "占位正文第三段" in result["body"]


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_is_clean(scraper: AutohomeScraper, mock_page: AsyncMock) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    body = result["body"]
    for chrome_text in (
        "首页导航占位",
        "下载APP占位",
        "侧边栏热门文章占位",
        "评论区占位",
        "关于汽车之家占位页脚",
    ):
        assert chrome_text not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_zh(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["language"] == "zh"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("networkidle timeout")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: AutohomeScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article_minimal.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    # No __NEXT_DATA__: title falls back to the cleaned <title> tag and
    # the body to the DOM paragraphs; the missing date degrades to "".
    assert result["title"] == "占位极简文章标题"
    assert result["body"] == "占位极简正文段落。"
    assert result["publish_date"] == ""


async def test_discover_articles_uses_wait_for(scraper: AutohomeScraper) -> None:
    listing_soup = scraper.parse_html(_fixture("listing.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = listing_soup
        await scraper.discover_articles()
    assert mock_fetch.await_count == len(AutohomeScraper._LISTING_PATHS)
    for call in mock_fetch.await_args_list:
        assert call.kwargs["wait_for"] == "ul.article"
        assert call.kwargs["wait_until"] == "domcontentloaded"


async def test_scrape_article_uses_wait_for(scraper: AutohomeScraper) -> None:
    article_soup = scraper.parse_html(_fixture("article.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = article_soup
        await scraper.scrape_article(ARTICLE_URL)
    mock_fetch.assert_awaited_once_with(
        ARTICLE_URL, wait_for="#parent-container", wait_until="domcontentloaded"
    )

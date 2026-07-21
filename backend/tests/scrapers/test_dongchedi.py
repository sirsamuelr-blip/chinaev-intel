"""Tests for scrapers.sources.dongchedi.DongchediScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scrapers.sources.dongchedi import DongchediScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dongchedi"

ARTICLE_URL = "https://www.dongchedi.com/article/7664000000000000001"

# listing.html feeds five unique articles through the JSON + DOM merge:
# two todayNews text items, two focusPic items, and one DOM-only anchor.
# Two video items, one image-only anchor, one /video/ link, and two
# duplicate entries (one JSON, one DOM) must all be skipped.
FIXTURE_UNIQUE_COUNT = 5

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
    """Read a fixture file from tests/fixtures/dongchedi/."""
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
async def scraper(mock_playwright_start: MagicMock) -> AsyncIterator[DongchediScraper]:
    """DongchediScraper entered with the fully mocked Playwright chain."""
    async with DongchediScraper() as instance:
        yield instance


def test_source_name() -> None:
    assert DongchediScraper.SOURCE_NAME == "dongchedi"


def test_base_url() -> None:
    assert DongchediScraper.BASE_URL == "https://www.dongchedi.com"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("listing.html")
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    assert len(result) == FIXTURE_UNIQUE_COUNT
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
        # The homepage feed carries no dates; entries degrade to "".
        assert entry["publish_date"] == ""
    # Discovery is a single homepage fetch.
    assert mock_page.goto.await_count == 1


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("listing.html")
    result = await scraper.discover_articles()
    assert result
    urls = [entry["url"] for entry in result]
    for url in urls:
        assert url.startswith("https://www.dongchedi.com/article/")
        # Tracking query strings and fragments are stripped for dedup.
        assert "?" not in url
        assert "#" not in url
    # JSON-only (focusPic) and DOM-only entries both survive the merge.
    assert "https://www.dongchedi.com/article/7664000000000000005" in urls
    assert "https://www.dongchedi.com/article/7664000000000000007" in urls
    # Video items and the image-only anchor are skipped.
    excluded_gids = (
        "7664000000000000002",
        "7664000000000000004",
        "7664000000000000008",
        "7664000000000000009",
    )
    for excluded_gid in excluded_gids:
        assert f"https://www.dongchedi.com/article/{excluded_gid}" not in urls
    # Duplicate JSON and DOM entries were deduplicated by URL.
    assert len(urls) == len(set(urls))
    by_url = {entry["url"]: entry for entry in result}
    # The first occurrence (todayNews head_article) wins the title.
    assert by_url[ARTICLE_URL]["title"] == "占位头条一：某品牌城市NOA全量推送"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("domcontentloaded timeout")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert set(result.keys()) == ARTICLE_KEYS
    for key in ARTICLE_KEYS:
        assert result[key]
    assert result["source_name"] == "dongchedi"
    assert result["source_url"] == ARTICLE_URL
    # Title and date come from the __NEXT_DATA__ pageProps.article payload.
    assert result["title"] == "占位文章标题：某品牌城市NOA功能全量推送"
    # publish_time 1784376803 epoch seconds == 2026-07-18 12:13:23 UTC.
    assert result["publish_date"] == "2026-07-18T12:13:23+00:00"
    assert "占位正文第一段" in result["body"]
    assert "占位正文第三段" in result["body"]


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_is_clean(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    body = result["body"]
    for chrome_text in (
        "首页导航占位",
        "登录占位",
        "侧边栏排行榜占位",
        "评论区占位内容",
        "关于懂车帝占位页脚",
    ):
        assert chrome_text not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_zh(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result["language"] == "zh"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("domcontentloaded timeout")
    result = await scraper.scrape_article(ARTICLE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: DongchediScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("article_minimal.html")
    result = await scraper.scrape_article(ARTICLE_URL)
    # No __NEXT_DATA__: title falls back to the <h1>, the body to the
    # article#article paragraphs; the missing timestamp degrades to "".
    assert result["title"] == "占位极简文章标题"
    assert result["body"] == "占位极简正文段落。"
    assert result["publish_date"] == ""


async def test_scrape_article_dom_fallback_without_json(scraper: DongchediScraper) -> None:
    article_soup = scraper.parse_html(_fixture("article.html"))
    next_data = article_soup.select_one("script#__NEXT_DATA__")
    assert next_data is not None
    next_data.decompose()
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = article_soup
        result = await scraper.scrape_article(ARTICLE_URL)
    # Title from the <h1>, body from article#article, and the date from
    # the visible span.time Beijing timestamp (2026-07-18 20:13 CST).
    assert result["title"] == "占位文章标题：某品牌城市NOA功能全量推送"
    assert "占位正文第二段" in result["body"]
    assert result["publish_date"] == "2026-07-18T12:13:00+00:00"


async def test_discover_articles_uses_wait_for(scraper: DongchediScraper) -> None:
    listing_soup = scraper.parse_html(_fixture("listing.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = listing_soup
        await scraper.discover_articles()
    mock_fetch.assert_awaited_once_with(
        "https://www.dongchedi.com/",
        wait_for='a[href*="/article/"]',
        wait_until="domcontentloaded",
    )


async def test_scrape_article_uses_wait_for(scraper: DongchediScraper) -> None:
    article_soup = scraper.parse_html(_fixture("article.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = article_soup
        await scraper.scrape_article(ARTICLE_URL)
    mock_fetch.assert_awaited_once_with(
        ARTICLE_URL, wait_for="#article", wait_until="domcontentloaded"
    )

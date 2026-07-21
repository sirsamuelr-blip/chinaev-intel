"""Tests for scrapers.sources.xiaohongshu.XiaoHongShuScraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scrapers.sources.xiaohongshu import XiaoHongShuScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "xiaohongshu"

NOTE_URL = "https://www.xiaohongshu.com/explore/66f000000000000000000a01"

# Discovery merges one search page and the explore feed into six unique
# notes: a01 + a02 (search JSON), a03 (search DOM), b01 + b03 (explore
# JSON, EV-filtered), and b04 (explore DOM). Live items, an id-less JSON
# entry, image-only cards, two non-EV explore notes, and the a01
# duplicate card on the explore page must all be dropped.
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
    """Read a fixture file from tests/fixtures/xiaohongshu/."""
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
async def scraper(mock_playwright_start: MagicMock) -> AsyncIterator[XiaoHongShuScraper]:
    """XiaoHongShuScraper entered with the fully mocked Playwright chain."""
    async with XiaoHongShuScraper() as instance:
        yield instance


def test_source_name() -> None:
    assert XiaoHongShuScraper.SOURCE_NAME == "xiaohongshu"


def test_base_url() -> None:
    assert XiaoHongShuScraper.BASE_URL == "https://www.xiaohongshu.com"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_list(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.side_effect = [
        _fixture("search_results.html"),
        _fixture("explore_feed.html"),
    ]
    result = await scraper.discover_articles()
    assert isinstance(result, list)
    assert len(result) == FIXTURE_UNIQUE_COUNT
    for entry in result:
        assert set(entry.keys()) == {"url", "title", "publish_date"}
        assert entry["title"]
        # Neither listing carries dates; entries degrade to "".
        assert entry["publish_date"] == ""
    # Discovery is exactly two fetches: one search page, one explore feed.
    assert mock_page.goto.await_count == 2


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_extracts_urls(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.side_effect = [
        _fixture("search_results.html"),
        _fixture("explore_feed.html"),
    ]
    result = await scraper.discover_articles()
    assert result
    urls = [entry["url"] for entry in result]
    for url in urls:
        assert url.startswith("https://www.xiaohongshu.com/explore/")
        # Discovery returns tokenless canonical URLs as stable dedup keys.
        assert "?" not in url
        assert "#" not in url
    # DOM-only entries survive the JSON + DOM merge on both pages.
    assert "https://www.xiaohongshu.com/explore/66f000000000000000000a03" in urls
    assert "https://www.xiaohongshu.com/explore/66f000000000000000000b04" in urls
    # Live items, the id-less JSON entry, and non-EV explore notes are dropped.
    excluded_ids = (
        "66f000000000000000000a09",
        "66f000000000000000000b02",
        "66f000000000000000000b05",
        "66f000000000000000000b07",
    )
    for excluded_id in excluded_ids:
        assert f"https://www.xiaohongshu.com/explore/{excluded_id}" not in urls
    # The a01 duplicate card on the explore page was deduplicated by URL.
    assert len(urls) == len(set(urls))
    by_url = {entry["url"]: entry for entry in result}
    # The first occurrence (search JSON) wins the title.
    assert by_url[NOTE_URL]["title"] == "理想L6提车一个月真实体验"


@pytest.mark.usefixtures("mock_sleep")
async def test_discover_articles_returns_empty_on_failure(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("domcontentloaded timeout")
    result = await scraper.discover_articles()
    assert result == []


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_extracts_fields(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("post.html")
    result = await scraper.scrape_article(NOTE_URL)
    assert set(result.keys()) == ARTICLE_KEYS
    for key in ARTICLE_KEYS:
        assert result[key]
    assert result["source_name"] == "xiaohongshu"
    assert result["source_url"] == NOTE_URL
    # Title and date come from the __INITIAL_STATE__ noteDetailMap payload.
    assert result["title"] == "理想L6提车一个月真实体验"
    # time 1784376803000 epoch milliseconds == 2026-07-18 12:13:23 UTC.
    assert result["publish_date"] == "2026-07-18T12:13:23+00:00"
    assert "占位正文第一行" in result["body"]
    assert "占位正文第三行" in result["body"]


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_body_is_clean(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("post.html")
    result = await scraper.scrape_article(NOTE_URL)
    body = result["body"]
    for chrome_text in (
        "首页导航占位",
        "登录占位",
        "小红书占位作者",
        "点赞占位",
        "收藏占位",
        "评论区占位内容",
        "关于小红书占位页脚",
    ):
        assert chrome_text not in body


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_language_is_zh(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("post.html")
    result = await scraper.scrape_article(NOTE_URL)
    assert result["language"] == "zh"


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_scrape_date_is_iso(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("post.html")
    result = await scraper.scrape_article(NOTE_URL)
    parsed = datetime.fromisoformat(result["scrape_date"])
    assert parsed.tzinfo is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_returns_empty_on_fetch_failure(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    # Covers hard failures and the 404/login shells alike: #noteContainer
    # never appears there, so the selector wait times out.
    mock_page.goto.side_effect = PlaywrightTimeoutError("domcontentloaded timeout")
    result = await scraper.scrape_article(NOTE_URL)
    assert result == {}


@pytest.mark.usefixtures("mock_sleep")
async def test_scrape_article_handles_minimal_html(
    scraper: XiaoHongShuScraper, mock_page: AsyncMock
) -> None:
    mock_page.content.return_value = _fixture("post_minimal.html")
    result = await scraper.scrape_article(NOTE_URL)
    # No __INITIAL_STATE__: title falls back to #detail-title, the body
    # to #detail-desc; the missing date stamp degrades to "".
    assert result["title"] == "占位极简笔记标题"
    assert result["body"] == "占位极简笔记正文。"
    assert result["publish_date"] == ""


async def test_scrape_article_dom_fallback_without_json(scraper: XiaoHongShuScraper) -> None:
    post_soup = scraper.parse_html(_fixture("post.html"))
    state_script = None
    for script in post_soup.find_all("script"):
        if script.string and "__INITIAL_STATE__" in script.string:
            state_script = script
    assert state_script is not None
    state_script.decompose()
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = post_soup
        result = await scraper.scrape_article(NOTE_URL)
    # Title from #detail-title, body from #detail-desc span.note-text,
    # and the date from the visible span.date Beijing stamp (2026-07-18
    # CST midnight == 2026-07-17 16:00 UTC).
    assert result["title"] == "理想L6提车一个月真实体验"
    assert "占位正文第二行" in result["body"]
    assert result["publish_date"] == "2026-07-17T16:00:00+00:00"


async def test_scrape_article_title_falls_back_to_first_body_line(
    scraper: XiaoHongShuScraper,
) -> None:
    minimal_soup = scraper.parse_html(_fixture("post_minimal.html"))
    heading = minimal_soup.select_one("#detail-title")
    assert heading is not None
    heading.decompose()
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = minimal_soup
        result = await scraper.scrape_article(NOTE_URL)
    # Titles are optional on XiaoHongShu; the first body line stands in.
    assert result["title"] == "占位极简笔记正文。"


async def test_discover_articles_uses_wait_for(scraper: XiaoHongShuScraper) -> None:
    search_soup = scraper.parse_html(_fixture("search_results.html"))
    explore_soup = scraper.parse_html(_fixture("explore_feed.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [search_soup, explore_soup]
        await scraper.discover_articles()
    assert mock_fetch.await_count == 2
    search_call, explore_call = mock_fetch.await_args_list
    # Search: no wait_for — anonymous sessions get a resultless shell
    # where note cards never render, so waiting would only time out.
    search_url = search_call.args[0]
    assert search_url.startswith("https://www.xiaohongshu.com/search_result/?keyword=")
    assert "source=web_search_result_notes" in search_url
    assert search_call.kwargs == {"wait_until": "domcontentloaded"}
    assert explore_call.args == ("https://www.xiaohongshu.com/explore",)
    assert explore_call.kwargs == {
        "wait_for": "section.note-item",
        "wait_until": "domcontentloaded",
    }


async def test_scrape_article_uses_wait_for(scraper: XiaoHongShuScraper) -> None:
    post_soup = scraper.parse_html(_fixture("post.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = post_soup
        await scraper.scrape_article(NOTE_URL)
    mock_fetch.assert_awaited_once_with(
        NOTE_URL, wait_for="#noteContainer", wait_until="domcontentloaded"
    )


async def test_scrape_article_uses_cached_xsec_token(scraper: XiaoHongShuScraper) -> None:
    search_soup = scraper.parse_html(_fixture("search_results.html"))
    explore_soup = scraper.parse_html(_fixture("explore_feed.html"))
    post_soup = scraper.parse_html(_fixture("post.html"))
    with patch.object(scraper, "fetch_and_parse", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [search_soup, explore_soup]
        await scraper.discover_articles()
        mock_fetch.side_effect = None
        mock_fetch.return_value = post_soup
        result = await scraper.scrape_article(NOTE_URL)
    # The bare note URL 404s on the live site: the fetch re-attaches the
    # xsec_token cached during discovery, while the stored source_url
    # stays canonical (tokenless) so Firestore dedup keys are stable.
    assert mock_fetch.await_args is not None
    fetch_url = mock_fetch.await_args.args[0]
    assert fetch_url == f"{NOTE_URL}?xsec_token=ABtokenA01=&xsec_source=pc_feed"
    assert result["source_url"] == NOTE_URL

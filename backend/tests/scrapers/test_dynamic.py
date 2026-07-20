"""Tests for scrapers.dynamic.DynamicScraper."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config.settings import MAX_RETRIES
from scrapers.dynamic import DEFAULT_WAIT_TIMEOUT_MS, DynamicScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

PAGE_URL = "https://fake-dynamic.example.com/article/1"
PAGE_HTML = '<html><body><div class="content"><p>Rendered placeholder body</p></div></body></html>'


class FakeDynamicScraper(DynamicScraper):
    """Concrete DynamicScraper with stub article methods for testing."""

    SOURCE_NAME = "fake_dynamic"
    BASE_URL = "https://fake-dynamic.example.com"

    async def discover_articles(self) -> list[dict[str, str]]:
        """Stub: no discovery."""
        return []

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Stub: no scraping."""
        return {}


@pytest.fixture
def mock_sleep() -> Iterator[AsyncMock]:
    """Replace asyncio.sleep so tests never actually wait."""
    with patch("scrapers.base.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        yield sleep_mock


@pytest.fixture
def mock_page() -> AsyncMock:
    """Mocked Playwright page returning fixed HTML."""
    page = AsyncMock()
    page.content.return_value = PAGE_HTML
    return page


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
async def scraper(mock_playwright_start: MagicMock) -> AsyncIterator[FakeDynamicScraper]:
    """FakeDynamicScraper entered with the fully mocked Playwright chain."""
    async with FakeDynamicScraper() as instance:
        yield instance


@pytest.mark.usefixtures("scraper")
async def test_context_manager_launches_browser(mock_playwright: AsyncMock) -> None:
    mock_playwright.chromium.launch.assert_awaited_once_with(headless=True)


@pytest.mark.usefixtures("scraper")
async def test_context_manager_creates_context_with_ua(mock_browser: AsyncMock) -> None:
    mock_browser.new_context.assert_awaited_once()
    assert mock_browser.new_context.await_args is not None
    user_agent = mock_browser.new_context.await_args.kwargs["user_agent"]
    assert user_agent in FakeDynamicScraper._USER_AGENTS


@pytest.mark.usefixtures("mock_playwright_start")
async def test_context_manager_cleanup(
    mock_playwright: AsyncMock, mock_browser: AsyncMock, mock_browser_context: AsyncMock
) -> None:
    async with FakeDynamicScraper():
        pass
    mock_browser_context.close.assert_awaited_once()
    mock_browser.close.assert_awaited_once()
    mock_playwright.stop.assert_awaited_once()


@pytest.mark.usefixtures("mock_playwright_start")
async def test_context_manager_cleanup_handles_errors(
    mock_playwright: AsyncMock, mock_browser: AsyncMock
) -> None:
    mock_browser.close.side_effect = RuntimeError("browser already gone")
    async with FakeDynamicScraper():
        pass
    # The failing browser.close is swallowed and later steps still run.
    mock_playwright.stop.assert_awaited_once()


async def test_fetch_page_outside_context_raises() -> None:
    scraper = FakeDynamicScraper()
    with pytest.raises(RuntimeError, match="async context manager"):
        await scraper.fetch_page(PAGE_URL)


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_success(scraper: FakeDynamicScraper) -> None:
    result = await scraper.fetch_page(PAGE_URL)
    assert result == PAGE_HTML


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_opens_and_closes_page(
    scraper: FakeDynamicScraper, mock_browser_context: AsyncMock, mock_page: AsyncMock
) -> None:
    await scraper.fetch_page(PAGE_URL)
    mock_browser_context.new_page.assert_awaited_once()
    mock_page.close.assert_awaited_once()


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_waits_for_selector(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    await scraper.fetch_page(PAGE_URL, wait_for="div.content")
    mock_page.wait_for_selector.assert_awaited_once_with(
        "div.content", timeout=DEFAULT_WAIT_TIMEOUT_MS
    )


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_no_wait_for(scraper: FakeDynamicScraper, mock_page: AsyncMock) -> None:
    await scraper.fetch_page(PAGE_URL)
    mock_page.wait_for_selector.assert_not_awaited()


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_retries_on_failure(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = [PlaywrightTimeoutError("networkidle timeout"), None]
    result = await scraper.fetch_page(PAGE_URL)
    assert result == PAGE_HTML
    assert mock_page.goto.await_count == 2
    assert scraper.error_count == 1


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_returns_none_after_exhausted_retries(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("networkidle timeout")
    result = await scraper.fetch_page(PAGE_URL)
    assert result is None
    assert mock_page.goto.await_count == MAX_RETRIES


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_closes_page_on_error(
    scraper: FakeDynamicScraper, mock_browser_context: AsyncMock, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("networkidle timeout")
    await scraper.fetch_page(PAGE_URL)
    # Every opened page is closed again, even when navigation raises.
    assert mock_page.close.await_count == mock_browser_context.new_page.await_count


def test_parse_html_returns_soup() -> None:
    scraper = FakeDynamicScraper()
    soup = scraper.parse_html(PAGE_HTML)
    assert isinstance(soup, BeautifulSoup)
    paragraph = soup.find("p")
    assert paragraph is not None
    assert paragraph.get_text() == "Rendered placeholder body"


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_and_parse_success(scraper: FakeDynamicScraper) -> None:
    soup = await scraper.fetch_and_parse(PAGE_URL)
    assert isinstance(soup, BeautifulSoup)
    assert soup.select_one("div.content") is not None


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_and_parse_returns_none_on_failure(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    mock_page.goto.side_effect = PlaywrightTimeoutError("networkidle timeout")
    soup = await scraper.fetch_and_parse(PAGE_URL)
    assert soup is None


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_and_parse_passes_wait_params(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    await scraper.fetch_and_parse(PAGE_URL, wait_for="div.content", wait_timeout=5000.0)
    mock_page.goto.assert_awaited_once_with(PAGE_URL, wait_until="networkidle", timeout=5000.0)
    mock_page.wait_for_selector.assert_awaited_once_with("div.content", timeout=5000.0)


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_custom_wait_until(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    await scraper.fetch_page(PAGE_URL, wait_until="domcontentloaded")
    mock_page.goto.assert_awaited_once_with(
        PAGE_URL, wait_until="domcontentloaded", timeout=DEFAULT_WAIT_TIMEOUT_MS
    )


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_and_parse_custom_wait_until(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    await scraper.fetch_and_parse(PAGE_URL, wait_until="domcontentloaded")
    mock_page.goto.assert_awaited_once_with(
        PAGE_URL, wait_until="domcontentloaded", timeout=DEFAULT_WAIT_TIMEOUT_MS
    )


@pytest.mark.usefixtures("mock_sleep")
async def test_fetch_page_default_wait_until(
    scraper: FakeDynamicScraper, mock_page: AsyncMock
) -> None:
    await scraper.fetch_page(PAGE_URL)
    mock_page.goto.assert_awaited_once_with(
        PAGE_URL, wait_until="networkidle", timeout=DEFAULT_WAIT_TIMEOUT_MS
    )

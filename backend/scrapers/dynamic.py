"""DynamicScraper: async Playwright browser automation for JS-rendered sites.

Extends BaseScraper with a headless Chromium browser managed through the
async context manager protocol and BeautifulSoup HTML parsing. Source
scrapers for JS-rendered pages, infinite scroll, and login walls
(Autohome, Dongchedi, ...) extend this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrapers.base import BaseScraper

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import Browser, BrowserContext, Playwright

# Playwright timeouts are in milliseconds.
DEFAULT_WAIT_TIMEOUT_MS = 30000.0

# Load states accepted by page.goto(wait_until=...).
WaitUntilState = Literal["commit", "domcontentloaded", "load", "networkidle"]


class DynamicScraper(BaseScraper):
    """Scraper for JS-rendered sites using headless Chromium via Playwright.

    Still abstract: source scrapers must implement ``discover_articles``
    and ``scrape_article``. Use as an async context manager so the
    browser is launched and shut down cleanly::

        async with AutohomeScraper() as scraper:
            soup = await scraper.fetch_and_parse(url, wait_for="div.article")
    """

    def __init__(self) -> None:
        """Set up base scraper state and unopened browser slots."""
        super().__init__()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> Self:
        """Start Playwright, launch headless Chromium, and open a context."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(user_agent=self._get_random_ua())
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the context, browser, and Playwright driver.

        Each step is attempted even if an earlier one fails; cleanup
        errors are logged, never raised.
        """
        if self._context is not None:
            await self._close_quietly("browser context", self._context.close)
            self._context = None
        if self._browser is not None:
            await self._close_quietly("browser", self._browser.close)
            self._browser = None
        if self._playwright is not None:
            await self._close_quietly("playwright driver", self._playwright.stop)
            self._playwright = None

    async def _close_quietly(self, name: str, close_func: Callable[[], Awaitable[None]]) -> None:
        """Run one cleanup call, logging instead of raising on failure."""
        try:
            await close_func()
        except Exception as exc:  # cleanup must never crash on exit
            self.logger.warning(f"[{self.SOURCE_NAME}] error closing {name}: {exc}")

    async def fetch_page(
        self,
        url: str,
        wait_for: str | None = None,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_MS,
        wait_until: WaitUntilState = "networkidle",
    ) -> str | None:
        """Fetch a fully rendered page with rate limiting, UA rotation, and retries.

        ``wait_until`` controls Playwright's page load strategy and
        defaults to ``"networkidle"``; source scrapers can pass
        ``"domcontentloaded"`` for sites with persistent background
        connections that never go idle. The page additionally waits for
        the ``wait_for`` CSS selector when one is given — the hook for
        source scrapers that need specific JS-rendered content to appear.
        Each call opens and closes its own page so no state leaks between
        requests. Returns the rendered HTML, or None once all retries
        have failed. Raises RuntimeError if called outside the async
        context manager.
        """
        if self._context is None:
            msg = f"{type(self).__name__} must be used as an async context manager"
            raise RuntimeError(msg)
        context = self._context

        async def _fetch(fetch_url: str) -> str:
            page = await context.new_page()
            try:
                await page.set_extra_http_headers({"User-Agent": self._get_random_ua()})
                await page.goto(fetch_url, wait_until=wait_until, timeout=wait_timeout)
                if wait_for is not None:
                    await page.wait_for_selector(wait_for, timeout=wait_timeout)
                content: str = await page.content()
            finally:
                await page.close()
            return content

        result: str | None = await self._request_with_retry(_fetch, url)
        return result

    def parse_html(self, html: str) -> BeautifulSoup:
        """Parse an HTML string into a BeautifulSoup tree (lxml parser)."""
        return BeautifulSoup(html, "lxml")

    async def fetch_and_parse(
        self,
        url: str,
        wait_for: str | None = None,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_MS,
        wait_until: WaitUntilState = "networkidle",
    ) -> BeautifulSoup | None:
        """Fetch a rendered page and parse it, or None if the fetch failed.

        ``wait_until`` controls Playwright's page load strategy and
        defaults to ``"networkidle"``; source scrapers can pass
        ``"domcontentloaded"`` for sites with persistent background
        connections that never go idle.
        """
        html = await self.fetch_page(
            url, wait_for=wait_for, wait_timeout=wait_timeout, wait_until=wait_until
        )
        if html is None:
            return None
        return self.parse_html(html)

"""StaticScraper: async HTTP fetching and parsing for static sites and RSS feeds.

Extends BaseScraper with an httpx.AsyncClient managed through the async
context manager protocol, BeautifulSoup HTML parsing, and RSS/Atom feed
parsing via feedparser. Source scrapers for static HTML sites and RSS
feeds (Gasgoo, CnEVPost, ...) extend this class.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Self

import feedparser
import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

REQUEST_TIMEOUT_SECONDS = 30.0


class StaticScraper(BaseScraper):
    """Scraper for static HTML sites and RSS feeds using httpx + BeautifulSoup.

    Still abstract: source scrapers must implement ``discover_articles``
    and ``scrape_article``. Use as an async context manager so the httpx
    client is opened and closed cleanly::

        async with GasgooScraper() as scraper:
            soup = await scraper.fetch_and_parse(url)
    """

    def __init__(self) -> None:
        """Set up base scraper state and an unopened HTTP client slot."""
        super().__init__()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        """Open the shared httpx client."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            follow_redirects=True,
            headers={"User-Agent": self._get_random_ua()},
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the httpx client if it was opened."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_page(self, url: str) -> str | None:
        """Fetch a page with rate limiting, UA rotation, and retries.

        Returns the response body, or None once all retries have failed.
        Raises RuntimeError if called outside the async context manager.
        """
        if self._client is None:
            msg = f"{type(self).__name__} must be used as an async context manager"
            raise RuntimeError(msg)
        client = self._client

        async def _fetch(fetch_url: str) -> str:
            response = await client.get(fetch_url, headers={"User-Agent": self._get_random_ua()})
            response.raise_for_status()
            return response.text

        result: str | None = await self._request_with_retry(_fetch, url)
        return result

    def parse_html(self, html: str) -> BeautifulSoup:
        """Parse an HTML string into a BeautifulSoup tree (lxml parser)."""
        return BeautifulSoup(html, "lxml")

    async def fetch_and_parse(self, url: str) -> BeautifulSoup | None:
        """Fetch a page and parse it, or None if the fetch failed."""
        html = await self.fetch_page(url)
        if html is None:
            return None
        return self.parse_html(html)

    async def fetch_feed(self, url: str) -> list[dict[str, str]] | None:
        """Fetch and parse an RSS/Atom feed.

        Returns one dict per entry with keys ``url``, ``title``, and
        ``publish_date`` (ISO 8601, empty string when the feed omits a
        date). Returns None if the fetch failed, and an empty list when
        the feed has no entries.
        """
        content = await self.fetch_page(url)
        if content is None:
            return None
        feed = feedparser.parse(content)
        entries: list[dict[str, str]] = []
        for entry in feed.entries:
            published = entry.get("published_parsed")
            publish_date = (
                datetime.fromtimestamp(calendar.timegm(published), tz=UTC).isoformat()
                if published is not None
                else ""
            )
            entries.append(
                {
                    "url": str(entry.get("link", "")),
                    "title": str(entry.get("title", "")),
                    "publish_date": publish_date,
                }
            )
        return entries

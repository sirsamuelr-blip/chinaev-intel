"""Gasgoo (autonews.gasgoo.com) source scraper.

Research notes (verified against the live site on 2026-07-18):

RSS discovery
    - The site's /rss page advertises per-category feeds at
      ``/api/rss?ClassId=<n>``: Market & Industry = 2, EV = 7,
      Interview & Commentary = 8, Report = 9, ICV = 11, Video = 17,
      Other = 19.
    - Each feed returns RSS 2.0 XML with the ~20 most recent items.
      Titles and links are CDATA-wrapped, ``<link>`` values are absolute
      URLs, and ``<pubDate>`` is RFC 822 GMT — all parsed cleanly by
      feedparser via ``StaticScraper.fetch_feed``.
    - The same article can appear in more than one category feed, so
      discovery dedupes by URL.

Article pages
    - Server-rendered Next.js HTML; no JS execution needed.
    - Exactly one ``<h1>`` per page: the headline.
    - Body text lives in a unique ``<div class="... article-content ...">``
      inside the page's single ``<article>`` element. The
      ``article-content`` class is the stable hook; the surrounding
      Tailwind utility classes are not.
    - Publish date is in ``<meta property="article:published_time">`` as a
      naive ISO 8601 timestamp in UTC (it matches the RSS pubDate in GMT).
    - Quirks: the breadcrumb ("Home / EV / ...") sits inside ``<article>``
      but outside ``.article-content``; body paragraphs can include
      "Image Source: ..." caption lines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from bs4 import Tag

from scrapers.static import StaticScraper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


class GasgooScraper(StaticScraper):
    """Scraper for Gasgoo's English automotive news site, using RSS discovery."""

    SOURCE_NAME = "gasgoo"
    BASE_URL = "https://autonews.gasgoo.com"

    # Category feeds advertised on https://autonews.gasgoo.com/rss, limited
    # to the categories relevant to EV/software intelligence.
    _FEED_CLASS_IDS: ClassVar[dict[str, int]] = {
        "market-industry": 2,
        "ev": 7,
        "icv": 11,
    }

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover recent article URLs from the category RSS feeds.

        Fetches each category feed, dedupes entries by URL (articles often
        appear in multiple category feeds), and skips feeds whose fetch
        failed. Returns dicts with ``url``, ``title``, and ``publish_date``
        (ISO 8601 or empty string).
        """
        discovered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for category, class_id in self._FEED_CLASS_IDS.items():
            feed_url = f"{self.BASE_URL}/api/rss?ClassId={class_id}"
            entries = await self.fetch_feed(feed_url)
            if entries is None:
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] feed fetch failed for category {category}: {feed_url}"
                )
                continue
            for entry in entries:
                url = entry["url"]
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                discovered.append(entry)
        self.logger.info(
            f"[{self.SOURCE_NAME}] discovered {len(discovered)} articles "
            f"from {len(self._FEED_CLASS_IDS)} feeds"
        )
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one Gasgoo article page.

        Returns the scraper-spec article dict, or an empty dict when the
        fetch fails or the page is missing a title or body.
        """
        soup = await self.fetch_and_parse(url)
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        title = self._extract_title(soup)
        body = self._extract_body(soup)
        if not title or not body:
            self.logger.warning(f"[{self.SOURCE_NAME}] missing title or body for {url}")
            return {}
        return {
            "source_name": self.SOURCE_NAME,
            "source_url": url,
            "title": title,
            "body": body,
            "publish_date": self._extract_publish_date(soup, url),
            "scrape_date": datetime.now(UTC).isoformat(),
            "language": "en",
            "raw_html": str(soup),
        }

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Return the headline from the page's single ``<h1>``, or empty string."""
        heading = soup.find("h1")
        if not isinstance(heading, Tag):
            return ""
        title: str = heading.get_text(strip=True)
        return title

    def _extract_body(self, soup: BeautifulSoup) -> str:
        """Return the article text from ``div.article-content``, or empty string.

        Selecting the content div directly excludes the site chrome around
        it (nav, breadcrumb, sidebar, footer).
        """
        container = soup.select_one("div.article-content")
        if container is None:
            return ""
        body: str = container.get_text("\n", strip=True)
        return body

    def _extract_publish_date(self, soup: BeautifulSoup, url: str) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        Gasgoo's ``article:published_time`` meta value is a naive ISO
        timestamp already in UTC, so the UTC offset is attached explicitly.
        """
        meta = soup.find("meta", attrs={"property": "article:published_time"})
        if not isinstance(meta, Tag):
            return ""
        content = meta.get("content")
        if not isinstance(content, str) or not content:
            return ""
        try:
            parsed = datetime.fromisoformat(content)
        except ValueError:
            self.logger.warning(
                f"[{self.SOURCE_NAME}] unparseable publish date {content!r} on {url}"
            )
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()

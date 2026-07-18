"""CnEVPost (cnevpost.com) source scraper.

Research notes (verified against the live site on 2026-07-18):

RSS discovery
    - Standard WordPress feed at ``https://cnevpost.com/feed/``, advertised
      in the homepage ``<link rel="alternate" type="application/rss+xml">``
      tag. A single feed covers all categories — no per-category fan-out.
    - The feed returns RSS 2.0 XML with the ~50 most recent items.
      ``<link>`` values are absolute date-based permalinks
      (``/YYYY/MM/DD/slug/``) and ``<pubDate>`` is RFC 822 with a +0000
      offset — parsed cleanly by feedparser via ``StaticScraper.fetch_feed``.
    - Item descriptions are teasers, not full text, so article pages must
      still be scraped.

Article pages
    - WordPress-rendered static HTML (WP Rocket cache); no JS needed.
    - Exactly one ``<h1 class="entry-title">`` per page: the headline.
    - Body text lives in a unique ``<div class="entry-content">`` inside
      the page's single ``<article>`` element.
    - Publish date is in ``<meta property="article:published_time">`` as a
      full ISO 8601 timestamp with a +00:00 offset (already
      timezone-aware, unlike Gasgoo's naive value). A ``<time>`` element
      also carries the date, but in Beijing time (+08:00).
    - Quirks: ``entry-content`` embeds site chrome that must be stripped
      from the body — ``div.subscription-container`` ("Join us on
      Telegram or Google News") and ``div.danpian-xiangguan-post`` (a
      related-article promo). Figure captions like "Credit: ..." remain
      part of the text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from bs4 import Tag

from scrapers.static import StaticScraper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


class CnEVPostScraper(StaticScraper):
    """Scraper for CnEVPost's English China-EV news site, using RSS discovery."""

    SOURCE_NAME = "cnevpost"
    BASE_URL = "https://cnevpost.com"

    _FEED_PATH: ClassVar[str] = "/feed/"

    # Chrome blocks embedded inside div.entry-content that are not article
    # text: subscription CTA and related-article promo.
    _BODY_CHROME_CLASSES: ClassVar[tuple[str, ...]] = (
        "subscription-container",
        "danpian-xiangguan-post",
    )

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover recent article URLs from the WordPress RSS feed.

        Returns dicts with ``url``, ``title``, and ``publish_date``
        (ISO 8601 or empty string). Returns an empty list when the feed
        fetch fails.
        """
        feed_url = f"{self.BASE_URL}{self._FEED_PATH}"
        entries = await self.fetch_feed(feed_url)
        if entries is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] feed fetch failed: {feed_url}")
            return []
        discovered = [entry for entry in entries if entry["url"]]
        self.logger.info(f"[{self.SOURCE_NAME}] discovered {len(discovered)} articles")
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one CnEVPost article page.

        Returns the scraper-spec article dict, or an empty dict when the
        fetch fails or the page is missing a title or body.
        """
        soup = await self.fetch_and_parse(url)
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        # Capture the original page before body extraction strips the
        # embedded chrome blocks out of the parse tree.
        raw_html = str(soup)
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
            "raw_html": raw_html,
        }

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Return the headline from ``h1.entry-title``, or empty string.

        Falls back to the page's first ``<h1>`` if the WordPress class
        ever changes.
        """
        heading = soup.find("h1", class_="entry-title") or soup.find("h1")
        if not isinstance(heading, Tag):
            return ""
        title: str = heading.get_text(strip=True)
        return title

    def _extract_body(self, soup: BeautifulSoup) -> str:
        """Return the article text from ``div.entry-content``, or empty string.

        Strips the subscription CTA and related-article promo blocks that
        WordPress embeds inside the content div; the surrounding site
        chrome (nav, sidebar, footer) is excluded by selecting the content
        div directly.
        """
        container = soup.select_one("div.entry-content")
        if container is None:
            return ""
        for chrome_class in self._BODY_CHROME_CLASSES:
            for element in container.find_all("div", class_=chrome_class):
                element.decompose()
        body: str = container.get_text("\n", strip=True)
        return body

    def _extract_publish_date(self, soup: BeautifulSoup, url: str) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        CnEVPost's ``article:published_time`` meta value is already
        timezone-aware (+00:00); it is normalized to UTC defensively in
        case the site ever emits a Beijing-time offset instead.
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
        return parsed.astimezone(UTC).isoformat()

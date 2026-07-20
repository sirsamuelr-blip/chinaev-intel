"""Autohome (autohome.com.cn) source scraper.

Research notes (verified against the live site on 2026-07-18 with headless
Chromium and a desktop Chrome User-Agent):

Anti-bot
    - No block encountered: the news, tech, and EV listing pages and the
      article pages all returned HTTP 200 with full content in headless
      Chromium — no CAPTCHA, interstitial, or bot-detection redirect.
      No evasion launch flags were needed.
    - Static httpx fetches currently succeed as well (the pages are
      server-rendered), but Autohome is historically volatile about JS
      rendering and anti-bot, so this scraper follows the spec and uses
      DynamicScraper. Fetches use ``goto(wait_until="domcontentloaded")``
      because ``networkidle`` intermittently hangs on persistent
      analytics/ad connections that never let the network settle; the
      ``wait_for`` CSS selector signals render completion instead.

Listing pages (/news/, /tech/, /ev/ — all share one structure)
    - Articles render inside ``ul.article`` as ``li[data-artidanchor]``
      items (60 per page). Ad slots are ``li`` elements without the
      ``data-artidanchor`` attribute, and sidebar widgets
      (``div.hot-article-wrap``, ``div.focusimg``) sit outside
      ``ul.article``, so both are excluded by the item selector.
    - Each item holds one ``a[href]`` wrapping the whole card. Hrefs are
      protocol-relative (``//www.autohome.com.cn/...``) and carry a
      ``#pvareaid=...`` tracking fragment that must be stripped for
      dedup. News/tech items link to ``/news/YYYYMM/NNNNNNN.html``; the
      EV section links to ``/article?id=<token>``. Both URL shapes render
      the same article page shell.
    - Title is the ``h3`` text. Date is ``span.fn-left`` — either a
      relative Beijing-time offset (10小时前, 2天前) or an absolute
      timestamp (2026-05-15 18:00:00).

Article pages (Next.js server-rendered app)
    - There is no ``<h1>``. The headline and date nodes carry only
      Tailwind arbitrary-value utility classes and the body wrapper class
      is CSS-module hashed (``style_Article_Wrap_Box__gg4q7``) — none of
      those are stable hooks.
    - Primary hook: the ``script#__NEXT_DATA__`` JSON blob at
      ``props.pageProps.articleContent`` with ``title``, ``publishDate``
      (naive Beijing time, e.g. "2026-07-18 20:13:23"), and ``content``
      (the article body HTML made of ``p.editor-paragraph`` and
      ``div.editor-image`` nodes).
    - Stable DOM fallbacks: container ``div#parent-container`` with body
      paragraphs ``p.editor-paragraph`` (semantic classes, not hashed),
      and the page ``<title>``, which wraps the headline as
      ``【图】<headline>_汽车之家``.
    - ``meta[property="websdk:release_date"]`` is an SDK build stamp,
      not the article publish date — do not use it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, ClassVar

from bs4 import Tag

from scrapers.dynamic import DynamicScraper

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bs4 import BeautifulSoup

# Autohome listing dates and article timestamps are Beijing time.
_CHINA_TZ = timezone(timedelta(hours=8))

_MINUTES_AGO_RE = re.compile(r"(\d+)分钟前")
_HOURS_AGO_RE = re.compile(r"(\d+)小时前")
_DAYS_AGO_RE = re.compile(r"(\d+)天前")
_ABSOLUTE_DATE_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

# The page <title> wraps the headline as 【图】<headline>_汽车之家.
_TITLE_PREFIX = "【图】"
_TITLE_SUFFIX = "_汽车之家"


class AutohomeScraper(DynamicScraper):
    """Scraper for Autohome's news, tech, and EV sections via headless Chromium."""

    SOURCE_NAME = "autohome"
    BASE_URL = "https://www.autohome.com.cn"

    _LISTING_PATHS: ClassVar[dict[str, str]] = {
        "news": "/news/",  # latest car news
        "tech": "/tech/",  # car tech and design section
        "ev": "/ev/",  # new energy section
    }

    # The rendered article list is the JS-render signal on listing pages.
    _LISTING_WAIT_SELECTOR: ClassVar[str] = "ul.article"

    # Stable id wrapping the article body in the Next.js article shell.
    _ARTICLE_WAIT_SELECTOR: ClassVar[str] = "#parent-container"

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover recent article URLs from the news, tech, and EV listings.

        Fetches each listing page, dedupes entries by URL across sections
        (the same article can appear in more than one listing), and skips
        sections whose fetch failed. Returns dicts with ``url``,
        ``title``, and ``publish_date`` (ISO 8601 UTC or empty string).
        """
        discovered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for section, path in self._LISTING_PATHS.items():
            soup = await self.fetch_and_parse(
                f"{self.BASE_URL}{path}",
                wait_for=self._LISTING_WAIT_SELECTOR,
                wait_until="domcontentloaded",
            )
            if soup is None:
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] listing fetch failed for section {section}"
                )
                continue
            entries = self._parse_listing(soup)
            added = 0
            for entry in entries:
                if entry["url"] in seen_urls:
                    continue
                seen_urls.add(entry["url"])
                discovered.append(entry)
                added += 1
            self.logger.info(
                f"[{self.SOURCE_NAME}] section {section}: {len(entries)} items, {added} new"
            )
        self.logger.info(
            f"[{self.SOURCE_NAME}] discovered {len(discovered)} unique articles "
            f"from {len(self._LISTING_PATHS)} listing pages"
        )
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one Autohome article page.

        Extracts from the ``__NEXT_DATA__`` JSON payload first, falling
        back to the stable DOM hooks. Returns the scraper-spec article
        dict, or an empty dict when the fetch fails or the page yields no
        title or body.
        """
        soup = await self.fetch_and_parse(
            url,
            wait_for=self._ARTICLE_WAIT_SELECTOR,
            wait_until="domcontentloaded",
        )
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        # Capture the original page before script stripping mutates the
        # tree; stripping keeps JS out of the fallback date text search.
        raw_html = str(soup)
        article_content = self._extract_article_content(soup, url)
        for element in soup.find_all(["script", "style", "noscript"]):
            element.decompose()
        title = self._extract_title(soup, article_content)
        body = self._extract_body(soup, article_content)
        if not title or not body:
            self.logger.warning(f"[{self.SOURCE_NAME}] missing title or body for {url}")
            return {}
        return {
            "source_name": self.SOURCE_NAME,
            "source_url": url,
            "title": title,
            "body": body,
            "publish_date": self._extract_publish_date(soup, article_content, url),
            "scrape_date": datetime.now(UTC).isoformat(),
            "language": "zh",
            "raw_html": raw_html,
        }

    def _parse_listing(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Extract article entries from one listing page.

        Only ``li[data-artidanchor]`` items count as articles; ad-slot
        ``li`` elements lack the attribute and sidebar widgets sit
        outside ``ul.article``. Items without a usable link or title are
        skipped.
        """
        entries: list[dict[str, str]] = []
        for item in soup.select("ul.article > li[data-artidanchor]"):
            link = item.select_one("a[href]")
            if link is None:
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            url = self._normalize_url(href)
            if not url:
                continue
            heading = item.find("h3")
            title: str = heading.get_text(strip=True) if isinstance(heading, Tag) else ""
            if not title:
                continue
            date_element = item.select_one("span.fn-left")
            date_text: str = date_element.get_text(strip=True) if date_element else ""
            entries.append(
                {"url": url, "title": title, "publish_date": self._parse_listing_date(date_text)}
            )
        return entries

    def _normalize_url(self, href: str) -> str:
        """Return an absolute Autohome URL with the tracking fragment stripped.

        Listing hrefs are protocol-relative and end in a ``#pvareaid=...``
        fragment that would break URL dedup. Non-Autohome and non-HTTP
        links return an empty string.
        """
        url = href.split("#", 1)[0].strip()
        if not url:
            return ""
        if url.startswith("//"):
            url = f"https:{url}"
        elif url.startswith("/"):
            url = f"{self.BASE_URL}{url}"
        if not url.startswith(("http://", "https://")) or "autohome.com.cn" not in url:
            return ""
        return url

    def _extract_article_content(self, soup: BeautifulSoup, url: str) -> dict[str, str]:
        """Return the articleContent mapping from ``script#__NEXT_DATA__``, or {}.

        A missing script, malformed JSON, or unexpected shape degrades to
        an empty mapping so the DOM fallbacks take over.
        """
        script = soup.select_one("script#__NEXT_DATA__")
        if script is None or not script.string:
            return {}
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            self.logger.warning(f"[{self.SOURCE_NAME}] malformed __NEXT_DATA__ JSON on {url}")
            return {}
        props = data.get("props") if isinstance(data, dict) else None
        page_props = props.get("pageProps") if isinstance(props, dict) else None
        content = page_props.get("articleContent") if isinstance(page_props, dict) else None
        if not isinstance(content, dict):
            return {}
        return {
            key: value
            for key, value in content.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _extract_title(self, soup: BeautifulSoup, article_content: dict[str, str]) -> str:
        """Return the headline from ``__NEXT_DATA__``, else the cleaned page ``<title>``."""
        title = article_content.get("title", "").strip()
        if title:
            return title
        title_tag = soup.find("title")
        if not isinstance(title_tag, Tag):
            return ""
        text: str = title_tag.get_text(strip=True)
        return text.removeprefix(_TITLE_PREFIX).removesuffix(_TITLE_SUFFIX).strip()

    def _extract_body(self, soup: BeautifulSoup, article_content: dict[str, str]) -> str:
        """Return the article text from the JSON body HTML, else DOM paragraphs.

        Both paths join editor paragraph texts only, which excludes the
        page chrome (header, sidebar, comments) entirely.
        """
        body_html = article_content.get("content", "")
        if body_html:
            text = self._join_paragraphs(self.parse_html(body_html).find_all("p"))
            if text:
                return text
        container = soup.select_one(self._ARTICLE_WAIT_SELECTOR)
        if container is None:
            return ""
        return self._join_paragraphs(container.select("p.editor-paragraph"))

    def _join_paragraphs(self, paragraphs: Iterable[Tag]) -> str:
        """Join paragraph texts into one newline-separated body string."""
        texts = (paragraph.get_text(" ", strip=True) for paragraph in paragraphs)
        return "\n".join(text for text in texts if text)

    def _extract_publish_date(
        self, soup: BeautifulSoup, article_content: dict[str, str], url: str
    ) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        The ``__NEXT_DATA__`` ``publishDate`` is naive Beijing time
        ("2026-07-18 20:13:23"). When it is missing or unparseable, fall
        back to the first absolute date in the page text (the visible
        article-header timestamp; scripts are stripped before this runs).
        """
        publish_date = article_content.get("publishDate", "")
        if publish_date:
            parsed = self._parse_absolute_date(publish_date)
            if parsed:
                return parsed
            self.logger.warning(
                f"[{self.SOURCE_NAME}] unparseable publish date {publish_date!r} on {url}"
            )
        return self._parse_absolute_date(soup.get_text(" ", strip=True))

    def _parse_listing_date(self, text: str) -> str:
        """Parse a listing date (relative or absolute Beijing time) to ISO 8601 UTC."""
        if not text:
            return ""
        now = datetime.now(UTC)
        match = _MINUTES_AGO_RE.search(text)
        if match:
            return (now - timedelta(minutes=int(match.group(1)))).isoformat()
        match = _HOURS_AGO_RE.search(text)
        if match:
            return (now - timedelta(hours=int(match.group(1)))).isoformat()
        match = _DAYS_AGO_RE.search(text)
        if match:
            return (now - timedelta(days=int(match.group(1)))).isoformat()
        if "昨天" in text:
            return (now - timedelta(days=1)).isoformat()
        return self._parse_absolute_date(text)

    def _parse_absolute_date(self, text: str) -> str:
        """Parse the first ``YYYY-MM-DD [HH:MM[:SS]]`` Beijing timestamp to ISO 8601 UTC."""
        match = _ABSOLUTE_DATE_RE.search(text)
        if match is None:
            return ""
        year, month, day, hour, minute, second = match.groups()
        try:
            stamped = datetime(
                int(year),
                int(month),
                int(day),
                int(hour or 0),
                int(minute or 0),
                int(second or 0),
                tzinfo=_CHINA_TZ,
            )
        except ValueError:
            return ""
        return stamped.astimezone(UTC).isoformat()

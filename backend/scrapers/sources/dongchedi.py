"""Dongchedi (dongchedi.com) source scraper.

Research notes (2026-07-21, headless Chromium from a US IP plus the site's
production Next.js chunks fetched from the ByteDance static CDN):

Anti-bot / login wall
    - The homepage renders fully in headless Chromium (HTTP 200, complete
      ``__NEXT_DATA__`` payload). Section listings (``/news``, ``/digest``,
      ``/newenergy``) and article pages (``/article/<gid>``) server-side
      redirected to a ``/login-required`` shell from a US IP; the mobile
      site (``m.dongchedi.com/article/...``) serves a hard login page.
      The production scrapers run from an HK/SG VPS where this gating may
      differ, so article extraction keeps both the JSON and DOM paths and
      degrades to an empty result (never a crash) when walled.
    - Fetches use ``goto(wait_until="domcontentloaded")`` — like Autohome,
      ByteDance analytics keep persistent connections that can stall
      ``networkidle`` — with a visible-element ``wait_for`` selector as
      the render signal.

Homepage (the one listing page reachable without login)
    - Next.js SSR app; ``script#__NEXT_DATA__`` carries the feed at
      ``props.pageProps``: ``todayNews.head_article[]`` and
      ``todayNews.content_article[]`` (``gid_str``, ``title``,
      ``is_video``, ``article_type``) plus ``focusPic[].pic_list[]``
      (``group_id``, ``title``, ``article_type``). Neither carries a
      publish date. ``article_type == 2`` / ``is_video == true`` are
      video items that route to ``/video/<gid>`` on the live site.
    - The rendered DOM additionally holds plain ``a[href*="/article/"]``
      anchors (feed cards and SEO footer links) whose text is the title.
      Discovery merges JSON and DOM entries and dedupes by URL, so a
      missing or malformed JSON payload degrades to anchor scraping.

Article pages (verified from the production ``/article`` route chunk on
the CDN — ``static/chunks/pages/article-*.js`` — since the page itself is
login-walled from this network)
    - ``getInitialProps`` fetches ``/motor/pc/common/article/detail
      ?group_id=<gid>`` and returns ``{article: data}``, so
      ``__NEXT_DATA__.props.pageProps.article`` holds ``title``,
      ``source`` (author/outlet), ``publish_time`` (unix epoch seconds),
      and ``content`` (body HTML, XSS-filtered then injected via
      dangerouslySetInnerHTML).
    - Stable DOM hooks: headline ``h1`` (class ``title``), timestamp
      ``span.time`` (Beijing local, "2026-07-18 20:13"), and the body
      container ``article#article`` (class ``article-content``) — the
      only element with that id. Sidebar (``div.leaderboard``) and
      comments (``div.comment``) sit outside it. Styled-jsx hash classes
      (``jsx-3870445071``) are build-specific and never used as hooks.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, ClassVar

from bs4 import Tag

from scrapers.dynamic import DynamicScraper

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from bs4 import BeautifulSoup

# Dongchedi renders article timestamps in Beijing local time.
_CHINA_TZ = timezone(timedelta(hours=8))

_ABSOLUTE_DATE_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

# Article hrefs are /article/<numeric gid>, optionally with ?zt=feed
# tracking query strings that must be stripped for dedup.
_ARTICLE_PATH_RE = re.compile(r"/article/(\d+)")


class DongchediScraper(DynamicScraper):
    """Scraper for Dongchedi's homepage feed and article pages via headless Chromium."""

    SOURCE_NAME = "dongchedi"
    BASE_URL = "https://www.dongchedi.com"

    # Server-rendered feed anchors signal the homepage has rendered.
    _LISTING_WAIT_SELECTOR: ClassVar[str] = 'a[href*="/article/"]'

    # The body container id on article pages; never appears on the
    # login-required shell, so a walled page times out into a clean
    # empty result instead of yielding chrome-only content.
    _ARTICLE_WAIT_SELECTOR: ClassVar[str] = "#article"

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover article URLs from the homepage feed.

        Merges the ``__NEXT_DATA__`` feed lists with the rendered
        ``a[href*="/article/"]`` anchors (JSON first), skipping video
        items and deduping by URL. Returns dicts with ``url``, ``title``,
        and ``publish_date`` (always empty: the homepage feed carries no
        dates; the article page supplies them).
        """
        soup = await self.fetch_and_parse(
            f"{self.BASE_URL}/",
            wait_for=self._LISTING_WAIT_SELECTOR,
            wait_until="domcontentloaded",
        )
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] homepage fetch failed")
            return []
        page_props = self._extract_page_props(soup, self.BASE_URL)
        discovered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for entry in [*self._json_entries(page_props), *self._dom_entries(soup)]:
            if entry["url"] in seen_urls:
                continue
            seen_urls.add(entry["url"])
            discovered.append(entry)
        self.logger.info(
            f"[{self.SOURCE_NAME}] discovered {len(discovered)} unique articles from homepage"
        )
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one Dongchedi article page.

        Extracts from the ``__NEXT_DATA__`` ``pageProps.article`` payload
        first, falling back to the stable DOM hooks. Returns the
        scraper-spec article dict, or an empty dict when the fetch fails
        (including the login-wall case) or the page yields no title or
        body.
        """
        soup = await self.fetch_and_parse(
            url,
            wait_for=self._ARTICLE_WAIT_SELECTOR,
            wait_until="domcontentloaded",
        )
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        # Capture the original page before script stripping mutates the tree.
        raw_html = str(soup)
        article_data = self._extract_article_data(soup, url)
        for element in soup.find_all(["script", "style", "noscript"]):
            element.decompose()
        title = self._extract_title(soup, article_data)
        body = self._extract_body(soup, article_data)
        if not title or not body:
            self.logger.warning(f"[{self.SOURCE_NAME}] missing title or body for {url}")
            return {}
        return {
            "source_name": self.SOURCE_NAME,
            "source_url": url,
            "title": title,
            "body": body,
            "publish_date": self._extract_publish_date(soup, article_data, url),
            "scrape_date": datetime.now(UTC).isoformat(),
            "language": "zh",
            "raw_html": raw_html,
        }

    def _extract_page_props(self, soup: BeautifulSoup, url: str) -> dict[str, object]:
        """Return the pageProps mapping from ``script#__NEXT_DATA__``, or {}.

        A missing script, malformed JSON, or unexpected shape degrades to
        an empty mapping so the DOM paths take over.
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
        if not isinstance(page_props, dict):
            return {}
        return {key: value for key, value in page_props.items() if isinstance(key, str)}

    def _json_entries(self, page_props: dict[str, object]) -> list[dict[str, str]]:
        """Build listing entries from the homepage ``__NEXT_DATA__`` feed lists."""
        entries: list[dict[str, str]] = []
        for item in self._iter_feed_items(page_props):
            if item.get("is_video") is True or item.get("article_type") == 2:
                continue
            gid = item.get("gid_str") or item.get("group_id")
            title = item.get("title")
            if not isinstance(gid, str) or not gid.isdigit():
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            entries.append(
                {
                    "url": f"{self.BASE_URL}/article/{gid}",
                    "title": title.strip(),
                    "publish_date": "",
                }
            )
        return entries

    def _iter_feed_items(self, page_props: dict[str, object]) -> Iterator[dict[str, object]]:
        """Yield raw feed item dicts from todayNews and focusPic, shape-guarded."""
        today_news = page_props.get("todayNews")
        if isinstance(today_news, dict):
            for key in ("head_article", "content_article"):
                items = today_news.get(key)
                if isinstance(items, list):
                    yield from (item for item in items if isinstance(item, dict))
        focus_pic = page_props.get("focusPic")
        if isinstance(focus_pic, list):
            for block in focus_pic:
                pic_list = block.get("pic_list") if isinstance(block, dict) else None
                if isinstance(pic_list, list):
                    yield from (item for item in pic_list if isinstance(item, dict))

    def _dom_entries(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Build listing entries from rendered ``/article/`` anchors.

        Anchors without link text (image-only cards) are skipped; hrefs
        are normalized to absolute URLs with tracking queries stripped.
        """
        entries: list[dict[str, str]] = []
        for link in soup.select('a[href*="/article/"]'):
            href = link.get("href")
            if not isinstance(href, str):
                continue
            match = _ARTICLE_PATH_RE.search(href)
            if match is None:
                continue
            title = link.get_text(strip=True)
            if not title:
                continue
            entries.append(
                {
                    "url": f"{self.BASE_URL}/article/{match.group(1)}",
                    "title": title,
                    "publish_date": "",
                }
            )
        return entries

    def _extract_article_data(self, soup: BeautifulSoup, url: str) -> dict[str, object]:
        """Return ``pageProps.article`` from ``__NEXT_DATA__``, or {}."""
        article = self._extract_page_props(soup, url).get("article")
        if not isinstance(article, dict):
            return {}
        return {key: value for key, value in article.items() if isinstance(key, str)}

    def _extract_title(self, soup: BeautifulSoup, article_data: dict[str, object]) -> str:
        """Return the headline from ``__NEXT_DATA__``, else the page ``<h1>``."""
        title = article_data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        heading = soup.find("h1")
        if not isinstance(heading, Tag):
            return ""
        heading_text: str = heading.get_text(strip=True)
        return heading_text

    def _extract_body(self, soup: BeautifulSoup, article_data: dict[str, object]) -> str:
        """Return the article text from the JSON body HTML, else ``article#article``.

        Both paths take paragraph texts only, which drops image blocks
        and excludes the page chrome (header, sidebar leaderboard,
        comments) entirely; a paragraph-free body degrades to the
        container's full text.
        """
        content = article_data.get("content")
        if isinstance(content, str) and content:
            text = self._join_paragraphs(self.parse_html(content).find_all("p"))
            if text:
                return text
        container = soup.select_one(self._ARTICLE_WAIT_SELECTOR)
        if container is None:
            return ""
        text = self._join_paragraphs(container.find_all("p"))
        if text:
            return text
        container_text: str = container.get_text(" ", strip=True)
        return container_text

    def _join_paragraphs(self, paragraphs: Iterable[Tag]) -> str:
        """Join paragraph texts into one newline-separated body string."""
        texts = (paragraph.get_text(" ", strip=True) for paragraph in paragraphs)
        return "\n".join(text for text in texts if text)

    def _extract_publish_date(
        self, soup: BeautifulSoup, article_data: dict[str, object], url: str
    ) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        The ``__NEXT_DATA__`` ``publish_time`` is unix epoch seconds.
        When missing or unusable, fall back to the visible ``span.time``
        Beijing-local timestamp.
        """
        publish_time = article_data.get("publish_time")
        if isinstance(publish_time, str) and publish_time.isdigit():
            publish_time = int(publish_time)
        if isinstance(publish_time, int | float) and publish_time > 0:
            try:
                return datetime.fromtimestamp(publish_time, tz=UTC).isoformat()
            except (OverflowError, OSError, ValueError):
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] unusable publish_time {publish_time!r} on {url}"
                )
        time_element = soup.select_one("span.time")
        if time_element is None:
            return ""
        return self._parse_absolute_date(time_element.get_text(strip=True))

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

"""36kr (36kr.com) source scraper.

Research notes (verified against the live site on 2026-07-21):

Listing discovery
    - The automobile channel lives at ``/information/travel/`` — the
      site's nav labels the ``travel`` key 汽车. The ``/information/
      auto_car/`` path mentioned in early planning returns 404.
    - Pages are server-rendered and embed their full data as a
      ``window.initialState=`` JSON assignment that shares one
      ``<script>`` tag with other globals (``window.__GATEWAY_SIGN__``),
      so extraction scans the raw page text and decodes exactly one JSON
      value from the assignment position.
    - Listing items are at ``information.informationList.itemList[*]``:
      ``itemId``, ``route`` ("detail_article?itemId=..."), and
      ``templateMaterial`` with ``widgetTitle`` and ``publishTime``
      (epoch milliseconds). Non-article cards (videos, themes) carry
      other route prefixes and are skipped. Article URLs are
      ``https://36kr.com/p/<itemId>``.
    - The same cards are also server-rendered as ``a.article-item-title``
      links (30 per page, no dates); they are the DOM fallback when the
      JSON is missing or malformed.

Article pages
    - Same ``window.initialState`` embedding, with the article at
      ``articleDetail.articleDetailData.data``: ``widgetTitle``,
      ``publishTime`` (epoch ms), and ``widgetContent`` (body HTML of
      plain ``<p>`` nodes).
    - Stable DOM fallbacks: ``h1.article-title`` for the headline and
      ``div.articleDetailContent`` for the body. The surrounding
      ``div.article-content`` also wraps the title/author/date header, so
      it is not used directly. ``meta[property="article:published_time"]``
      carries a Beijing-time (+08:00) ISO timestamp, normalized to UTC as
      the date fallback.
    - Quirks: republished articles end with an attribution paragraph
      ("本文来自微信公众号...36氪经授权发布"), which is kept as body text.
      Ads, related-article promos, comments, and the sidebar all sit
      outside ``div.articleDetailContent`` and never reach the body.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from bs4 import Tag

from scrapers.static import StaticScraper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

# 36kr's visible timestamps and meta dates are Beijing time.
_CHINA_TZ = timezone(timedelta(hours=8))

_INITIAL_STATE_MARKER = "window.initialState="
_ARTICLE_ROUTE_PREFIX = "detail_article"


class ThirtySixKrScraper(StaticScraper):
    """Scraper for 36kr's automobile channel via embedded initialState JSON."""

    SOURCE_NAME = "36kr"
    BASE_URL = "https://36kr.com"

    # The 汽车 (automobile) information channel.
    _LISTING_PATH: ClassVar[str] = "/information/travel/"

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover recent article URLs from the automobile channel listing.

        Parses the embedded initialState JSON first and falls back to the
        server-rendered listing cards. Returns dicts with ``url``,
        ``title``, and ``publish_date`` (ISO 8601 UTC or empty string),
        deduped by URL; an empty list when the listing fetch fails.
        """
        listing_url = f"{self.BASE_URL}{self._LISTING_PATH}"
        html = await self.fetch_page(listing_url)
        if html is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] listing fetch failed: {listing_url}")
            return []
        entries = self._parse_listing_state(self._extract_initial_state(html, listing_url))
        if not entries:
            entries = self._parse_listing_dom(self.parse_html(html))
        discovered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for entry in entries:
            if entry["url"] in seen_urls:
                continue
            seen_urls.add(entry["url"])
            discovered.append(entry)
        self.logger.info(f"[{self.SOURCE_NAME}] discovered {len(discovered)} articles")
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one 36kr article page.

        Extracts from the initialState JSON payload first, falling back
        to the stable DOM hooks. Returns the scraper-spec article dict,
        or an empty dict when the fetch fails or the page yields no title
        or body.
        """
        html = await self.fetch_page(url)
        if html is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        soup = self.parse_html(html)
        data = self._extract_article_data(html, url)
        title = self._extract_title(soup, data)
        body = self._extract_body(soup, data)
        if not title or not body:
            self.logger.warning(f"[{self.SOURCE_NAME}] missing title or body for {url}")
            return {}
        return {
            "source_name": self.SOURCE_NAME,
            "source_url": url,
            "title": title,
            "body": body,
            "publish_date": self._extract_publish_date(soup, data, url),
            "scrape_date": datetime.now(UTC).isoformat(),
            "language": "zh",
            "raw_html": html,
        }

    def _extract_initial_state(self, html: str, url: str) -> dict[str, Any]:
        """Return the decoded ``window.initialState`` object, or {}.

        The assignment shares its script tag with other globals, so this
        scans the raw page text and decodes exactly one JSON value from
        the assignment position. dict[str, Any]: the payload mixes
        strings, numbers, lists, and nested objects.
        """
        index = html.find(_INITIAL_STATE_MARKER)
        if index == -1:
            self.logger.warning(f"[{self.SOURCE_NAME}] no initialState payload on {url}")
            return {}
        payload = html[index + len(_INITIAL_STATE_MARKER) :].lstrip()
        try:
            state, _ = json.JSONDecoder().raw_decode(payload)
        except json.JSONDecodeError:
            self.logger.warning(f"[{self.SOURCE_NAME}] malformed initialState JSON on {url}")
            return {}
        if not isinstance(state, dict):
            return {}
        return {key: value for key, value in state.items() if isinstance(key, str)}

    def _parse_listing_state(self, state: dict[str, Any]) -> list[dict[str, str]]:
        """Extract article entries from the listing initialState, or [].

        Only items whose ``route`` targets an article are kept; video,
        live, and theme cards use other route prefixes.
        """
        information = state.get("information")
        info_list = information.get("informationList") if isinstance(information, dict) else None
        item_list = info_list.get("itemList") if isinstance(info_list, dict) else None
        if not isinstance(item_list, list):
            return []
        entries: list[dict[str, str]] = []
        for item in item_list:
            if not isinstance(item, dict):
                continue
            route = item.get("route")
            if not isinstance(route, str) or not route.startswith(_ARTICLE_ROUTE_PREFIX):
                continue
            item_id = item.get("itemId")
            if not isinstance(item_id, int):
                continue
            material = item.get("templateMaterial")
            if not isinstance(material, dict):
                material = {}
            title = material.get("widgetTitle")
            entries.append(
                {
                    "url": f"{self.BASE_URL}/p/{item_id}",
                    "title": title if isinstance(title, str) else "",
                    "publish_date": self._epoch_ms_to_iso(material.get("publishTime")),
                }
            )
        return entries

    def _parse_listing_dom(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Extract article entries from the server-rendered listing cards.

        Fallback for when the initialState JSON is missing or malformed.
        The cards carry no dates, so ``publish_date`` is empty.
        """
        entries: list[dict[str, str]] = []
        for link in soup.select("a.article-item-title[href]"):
            href = link.get("href")
            if not isinstance(href, str):
                continue
            url = self._normalize_url(href)
            title = link.get_text(strip=True)
            if not url or not title:
                continue
            entries.append({"url": url, "title": title, "publish_date": ""})
        return entries

    def _normalize_url(self, href: str) -> str:
        """Return an absolute 36kr article URL, or empty string.

        Listing hrefs are site-relative (``/p/<itemId>``). Links that do
        not resolve to a 36kr article page return an empty string.
        """
        url = href.strip()
        if url.startswith("//"):
            url = f"https:{url}"
        elif url.startswith("/"):
            url = f"{self.BASE_URL}{url}"
        if not url.startswith(("http://", "https://")) or "36kr.com" not in url:
            return ""
        if "/p/" not in url:
            return ""
        return url

    def _extract_article_data(self, html: str, url: str) -> dict[str, Any]:
        """Return ``articleDetail.articleDetailData.data`` from initialState, or {}.

        dict[str, Any]: the article payload mixes strings and numbers.
        """
        state = self._extract_initial_state(html, url)
        detail = state.get("articleDetail")
        detail_data = detail.get("articleDetailData") if isinstance(detail, dict) else None
        data = detail_data.get("data") if isinstance(detail_data, dict) else None
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items() if isinstance(key, str)}

    def _extract_title(self, soup: BeautifulSoup, data: dict[str, Any]) -> str:
        """Return the headline from initialState, else ``h1.article-title``.

        Falls back to the page's first ``<h1>`` if the class ever changes.
        """
        title = data.get("widgetTitle")
        if isinstance(title, str) and title.strip():
            return title.strip()
        heading = soup.find("h1", class_="article-title") or soup.find("h1")
        if not isinstance(heading, Tag):
            return ""
        text: str = heading.get_text(strip=True)
        return text

    def _extract_body(self, soup: BeautifulSoup, data: dict[str, Any]) -> str:
        """Return the article text from the JSON body HTML, else the content div.

        Both hooks carry only body paragraphs, so the page chrome (nav,
        article header, ads, related articles, comments, sidebar, footer)
        is excluded without explicit stripping.
        """
        content_html = data.get("widgetContent")
        if isinstance(content_html, str) and content_html.strip():
            text: str = self.parse_html(content_html).get_text("\n", strip=True)
            if text:
                return text
        container = soup.select_one("div.articleDetailContent")
        if container is None:
            return ""
        body: str = container.get_text("\n", strip=True)
        return body

    def _extract_publish_date(self, soup: BeautifulSoup, data: dict[str, Any], url: str) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        Prefers the initialState ``publishTime`` epoch-milliseconds value;
        falls back to the ``article:published_time`` meta, whose Beijing
        (+08:00) timestamp is normalized to UTC. A naive meta value is
        assumed to be Beijing time.
        """
        stamped = self._epoch_ms_to_iso(data.get("publishTime"))
        if stamped:
            return stamped
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
            parsed = parsed.replace(tzinfo=_CHINA_TZ)
        return parsed.astimezone(UTC).isoformat()

    def _epoch_ms_to_iso(self, value: object) -> str:
        """Convert an epoch-milliseconds timestamp to ISO 8601 UTC, or empty string.

        Sub-second precision is dropped. bool is excluded explicitly
        because it is an int subclass.
        """
        if isinstance(value, bool) or not isinstance(value, int | float):
            return ""
        try:
            stamped = datetime.fromtimestamp(int(value) // 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return ""
        return stamped.isoformat()

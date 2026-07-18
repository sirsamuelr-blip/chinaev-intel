"""Baidu News (news.baidu.com) source scraper.

Research notes (verified against the live site on 2026-07-18):

Discovery
    - Baidu News has no RSS feed. The hash-route search UI at
      ``news.baidu.com/news#/search&keyword=...`` is a JS-rendered SPA and
      unusable for static scraping, and the news.baidu.com homepage is a
      category portal with no keyword targeting.
    - The classic search endpoint ``www.baidu.com/s?tn=news&word=<kw>``
      returns server-rendered HTML (page title 百度资讯搜索_<keyword>) and
      served full results to an httpx-style client with a browser
      User-Agent — no security-verification interstitial.
    - Results appear in two container shapes on the same page:
        * modern: ``div.result-op[tpl="news-normal"]`` — headline link in
          ``h3 a`` (the h3 class is hash-suffixed, e.g. news-title_1YtI1,
          so it is not a stable hook), source in ``span.c-color-gray``,
          date in ``span.c-color-gray2``
        * legacy: ``div.result`` — headline link in ``h3.c-title a``,
          source and date mixed together in the ``p.c-author`` text
    - Non-result widgets (hot-search sidebar ``tpl="right_toplist1"``,
      related-search ``srcid="rs"``) match neither container selector or
      carry no ``h3 a`` headline link, so they are skipped naturally.
    - Result links point either directly at external publishers
      (autohome.com.cn, chinapp.com, ...) or at Baidu properties
      (baijiahao.baidu.com, youjia.baidu.com). Both are kept as-is;
      redirects are not resolved at discovery time.
    - Listing dates are Beijing time (UTC+8) in mixed formats: relative
      (N分钟前, N小时前, 昨天, N天前) and absolute (7月7日, 2023年6月30日,
      2026年07月09日 14:34). All are normalized to ISO 8601 UTC;
      missing or unparseable dates become "".

Article pages
    - Search results link to many different publishers, so extraction is
      structure-agnostic with fallback chains:
        * title: first ``<h1>``, else ``og:title`` meta, else ``<title>``
        * body: first non-empty match of ``article``,
          ``div.article-content``, ``div.post-content``, ``#content``,
          ``div.content``; else the <div>/<section> whose direct <p>
          children hold the most text; else all <p> text on the page
        * publish date: ``article:published_time`` /
          ``og:article:published_time`` meta, else the first absolute
          date pattern found in the page text
    - Naive timestamps are assumed to be Beijing time (UTC+8), the norm
      for Chinese publishers, and converted to UTC.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from bs4 import Tag

from scrapers.static import StaticScraper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

# Baidu listing dates and Chinese publisher timestamps are Beijing time.
_CHINA_TZ = timezone(timedelta(hours=8))

_MINUTES_AGO_RE = re.compile(r"(\d+)分钟前")
_HOURS_AGO_RE = re.compile(r"(\d+)小时前")
_DAYS_AGO_RE = re.compile(r"(\d+)天前")
_FULL_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?")
_NUMERIC_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?")


class BaiduNewsScraper(StaticScraper):
    """Scraper for Baidu News keyword search results across external publishers."""

    SOURCE_NAME = "baidu_news"
    BASE_URL = "https://news.baidu.com"

    SEARCH_KEYWORDS: ClassVar[list[str]] = [
        "新能源汽车",  # new energy vehicles
        "智能驾驶",  # intelligent driving / ADAS
        "智能座舱",  # smart cockpit
        "自动驾驶",  # autonomous driving
        "电动汽车 软件",  # electric vehicle software
    ]

    # Server-rendered news search endpoint; the news.baidu.com search UI
    # itself is a JS-only SPA.
    _SEARCH_URL_TEMPLATE: ClassVar[str] = "https://www.baidu.com/s?tn=news&word={query}"

    # Both result-container shapes observed on the search results page.
    _RESULT_SELECTOR: ClassVar[str] = 'div.result-op[tpl="news-normal"], div.result'

    # Modern blocks carry the date in span.c-color-gray2; legacy blocks mix
    # source and date into the p.c-author text.
    _RESULT_DATE_SELECTOR: ClassVar[str] = "span.c-color-gray2, p.c-author"

    # Common article-body containers across the publishers Baidu links to,
    # tried in order before the largest-text-block fallback.
    _BODY_SELECTORS: ClassVar[tuple[str, ...]] = (
        "article",
        "div.article-content",
        "div.post-content",
        "#content",
        "div.content",
    )

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover article URLs by searching Baidu News for each keyword.

        Fetches one results page per keyword and dedupes entries by URL
        across keywords (the same story often matches several terms).
        Failed keyword fetches are logged and skipped. Returns dicts with
        ``url``, ``title``, and ``publish_date`` (ISO 8601 UTC or empty
        string).
        """
        discovered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for keyword in self.SEARCH_KEYWORDS:
            search_url = self._SEARCH_URL_TEMPLATE.format(query=quote(keyword))
            soup = await self.fetch_and_parse(search_url)
            if soup is None:
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] search fetch failed for keyword {keyword!r}"
                )
                continue
            entries = self._parse_search_results(soup)
            added = 0
            for entry in entries:
                if entry["url"] in seen_urls:
                    continue
                seen_urls.add(entry["url"])
                discovered.append(entry)
                added += 1
            self.logger.info(
                f"[{self.SOURCE_NAME}] keyword {keyword!r}: {len(entries)} results, {added} new"
            )
        self.logger.info(
            f"[{self.SOURCE_NAME}] discovered {len(discovered)} unique articles "
            f"from {len(self.SEARCH_KEYWORDS)} keyword searches"
        )
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one article page linked from Baidu News search results.

        The target can be any external publisher, so extraction relies on
        fallback chains rather than site-specific selectors. Returns the
        scraper-spec article dict, or an empty dict when the fetch fails
        or the page yields no title or body.
        """
        soup = await self.fetch_and_parse(url)
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        # Capture the original page before script/style stripping mutates
        # the tree; the stripping keeps JS and CSS out of the body
        # fallback and the page-text date search.
        raw_html = str(soup)
        for element in soup.find_all(["script", "style", "noscript"]):
            element.decompose()
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
            "language": "zh",
            "raw_html": raw_html,
        }

    def _parse_search_results(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Extract result entries from one search results page.

        Handles both the modern news-normal and legacy result containers.
        Blocks without an ``h3 a`` headline link (sidebar widgets) and
        non-HTTP links (protocol-relative Baidu internals) are skipped.
        """
        entries: list[dict[str, str]] = []
        for block in soup.select(self._RESULT_SELECTOR):
            link = block.select_one("h3 a[href]")
            if link is None:
                continue
            href = link.get("href")
            if not isinstance(href, str) or not href.startswith(("http://", "https://")):
                continue
            title: str = link.get_text(strip=True)
            if not title:
                continue
            date_element = block.select_one(self._RESULT_DATE_SELECTOR)
            date_text: str = date_element.get_text(" ", strip=True) if date_element else ""
            entries.append(
                {"url": href, "title": title, "publish_date": self._parse_result_date(date_text)}
            )
        return entries

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Return the headline via ``<h1>`` → og:title → ``<title>``, or empty string."""
        heading = soup.find("h1")
        if isinstance(heading, Tag):
            text: str = heading.get_text(strip=True)
            if text:
                return text
        meta = soup.find("meta", attrs={"property": "og:title"})
        if isinstance(meta, Tag):
            content = meta.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        title_tag = soup.find("title")
        if isinstance(title_tag, Tag):
            text = title_tag.get_text(strip=True)
            if text:
                return text
        return ""

    def _extract_body(self, soup: BeautifulSoup) -> str:
        """Return the article text via common body containers, or a text-block fallback."""
        for selector in self._BODY_SELECTORS:
            container = soup.select_one(selector)
            if container is not None:
                text: str = container.get_text("\n", strip=True)
                if text:
                    return text
        return self._extract_fallback_body(soup)

    def _extract_fallback_body(self, soup: BeautifulSoup) -> str:
        """Return the largest paragraph block on the page, or empty string.

        Scores each <div>/<section> by the text length of its direct <p>
        children (avoiding parent containers that swallow the whole page),
        and falls back to joining every <p> on the page.
        """
        best = ""
        for container in soup.find_all(["div", "section"]):
            text = "\n".join(
                paragraph.get_text(" ", strip=True)
                for paragraph in container.find_all("p", recursive=False)
            )
            if len(text) > len(best):
                best = text
        if best:
            return best
        paragraphs = (paragraph.get_text(" ", strip=True) for paragraph in soup.find_all("p"))
        return "\n".join(text for text in paragraphs if text)

    def _extract_publish_date(self, soup: BeautifulSoup, url: str) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        Tries the published-time meta properties first, then falls back
        to the first absolute date pattern in the page text. Naive meta
        timestamps are assumed to be Beijing time.
        """
        for prop in ("article:published_time", "og:article:published_time"):
            meta = soup.find("meta", attrs={"property": prop})
            if not isinstance(meta, Tag):
                continue
            content = meta.get("content")
            if not isinstance(content, str) or not content:
                continue
            try:
                parsed = datetime.fromisoformat(content)
            except ValueError:
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] unparseable publish date {content!r} on {url}"
                )
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_CHINA_TZ)
            return parsed.astimezone(UTC).isoformat()
        return self._parse_absolute_date(soup.get_text(" ", strip=True))

    def _parse_result_date(self, text: str) -> str:
        """Parse a listing date (relative or absolute) to ISO 8601 UTC, or empty string."""
        if not text:
            return ""
        return self._parse_relative_date(text) or self._parse_absolute_date(text)

    def _parse_relative_date(self, text: str) -> str:
        """Parse N分钟前/N小时前/N天前/昨天 offsets to ISO 8601 UTC, or empty string."""
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
        return ""

    def _parse_absolute_date(self, text: str) -> str:
        """Parse the first absolute Beijing-time date in ``text`` to ISO 8601 UTC.

        Handles 2026年7月9日 14:34, 2026-7-9 14:34, and year-less 7月7日
        (assigned to the current year, or the previous year if that would
        land in the future). Returns empty string when nothing parses.
        """
        match = _FULL_DATE_RE.search(text) or _NUMERIC_DATE_RE.search(text)
        if match:
            year, month, day, hour, minute = match.groups()
            stamped = self._build_utc(int(year), int(month), int(day), hour, minute)
            return stamped.isoformat() if stamped else ""
        match = _MONTH_DAY_RE.search(text)
        if match is None:
            return ""
        month, day, hour, minute = match.groups()
        now = datetime.now(_CHINA_TZ)
        stamped = self._build_utc(now.year, int(month), int(day), hour, minute)
        if stamped is not None and stamped > now.astimezone(UTC):
            stamped = self._build_utc(now.year - 1, int(month), int(day), hour, minute)
        return stamped.isoformat() if stamped else ""

    def _build_utc(
        self, year: int, month: int, day: int, hour: str | None, minute: str | None
    ) -> datetime | None:
        """Build a UTC datetime from Beijing-time parts, or None if invalid."""
        try:
            stamped = datetime(year, month, day, int(hour or 0), int(minute or 0), tzinfo=_CHINA_TZ)
        except ValueError:
            return None
        return stamped.astimezone(UTC)

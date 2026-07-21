"""XiaoHongShu (xiaohongshu.com) source scraper.

Research notes (2026-07-21, live probes with a desktop Chrome User-Agent
from a US IP; the production scrapers run from an HK/SG VPS where gating
may differ):

Access / anti-bot
    - The explore feed (``/explore``) is server-rendered and reachable
      WITHOUT login: ~25 note cards per response with full
      ``window.__INITIAL_STATE__`` data. No CAPTCHA or challenge was hit,
      but XiaoHongShu is the most aggressively bot-policed source in the
      project, so this scraper stretches the politeness delay to 10-15s
      (base class default is 5-10s) and keeps its request count per run
      minimal (one search page + one explore page).
    - Search result pages (``/search_result/?keyword=...``) render an
      EMPTY shell for anonymous sessions: ``__INITIAL_STATE__.search
      .feeds`` is ``[]`` and results are fetched client-side through
      signed API calls that require login. Search discovery is therefore
      attempted (one keyword per run, rotated daily) but expected to
      yield nothing until authentication or a friendlier egress exists;
      the explore feed filtered by EV title keywords is the reliable
      discovery path today.
    - Note URLs are only valid WITH their ``xsec_token`` query parameter:
      a bare ``/explore/<note_id>`` URL returns a "你访问的页面不见了"
      404 shell. Tokens come from the feed/search payloads and may not be
      stable across sessions, so discovery returns TOKENLESS canonical
      URLs (stable Firestore dedup keys) and caches ``note_id -> token``
      on the instance; ``scrape_article`` re-attaches the cached token to
      build the fetch URL. The runner discovers and scrapes on the same
      instance, so the cache holds for the whole run.

Embedded state (both page types)
    - ``window.__INITIAL_STATE__`` is a JS object literal, not strict
      JSON: it contains bare ``undefined`` values that must be rewritten
      to ``null`` before parsing.
    - Explore feed: ``feed.feeds[]`` items carry ``id``, ``xsecToken``,
      ``modelType`` ("note"), and ``noteCard`` with ``type``
      ("normal"/"video"), ``displayTitle``, and user info. No publish
      dates. Video-type notes are kept: they are regular ``/explore/``
      notes whose text (title + desc) is still extractable.
    - Search page: ``search.feeds[]`` uses the same item shape (empty for
      anonymous sessions).
    - Note pages: ``note.noteDetailMap[<note_id>].note`` carries
      ``noteId``, ``title`` (may be empty — titles are optional on
      XiaoHongShu), ``desc`` (the post body, including ``#..[话题]#`` tag
      markup and ``[emoji]`` codes typed by the author), ``time``
      (epoch milliseconds), and ``type``.

Stable DOM hooks (fallbacks when the JSON is missing/malformed)
    - Explore/search cards: ``section.note-item`` with the title and the
      tokenized href on one ``a.title`` anchor; ``data-v-*`` attributes
      and style hashes are build-specific and never used.
    - Note pages: container ``div#noteContainer`` (present for both
      normal and video notes; absent on the 404/login shells, so it
      doubles as the ``wait_for`` render signal), headline
      ``div#detail-title``, body ``div#detail-desc`` (with inner
      ``span.note-text``), and the visible ``span.date`` stamp — either
      absolute ("2026-07-18", optionally with a trailing location),
      month-day ("07-18"), or relative ("3 天前", "昨天 22:38").
      Author info, like counts, and comments live outside
      ``#detail-desc`` and are excluded by scoping.
    - Infinite scroll is deliberately not driven: each fetch takes the
      server-rendered first page only, keeping the request footprint low.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from bs4 import Tag

from scrapers.dynamic import DynamicScraper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

# XiaoHongShu renders visible note dates in Beijing local time.
_CHINA_TZ = timezone(timedelta(hours=8))

_ABSOLUTE_DATE_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
# Month-day stamps ("07-18") on recent notes omit the year.
_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)")
_MINUTES_AGO_RE = re.compile(r"(\d+)\s*分钟前")
_HOURS_AGO_RE = re.compile(r"(\d+)\s*小时前")
_DAYS_AGO_RE = re.compile(r"(\d+)\s*天前")

# Note ids are lowercase hex strings (24 chars today; range kept loose).
_NOTE_ID_RE = re.compile(r"[0-9a-f]{16,32}")
_NOTE_PATH_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{16,32})")
_XSEC_TOKEN_RE = re.compile(r"[?&]xsec_token=([^&#]+)")

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*\})", re.DOTALL)
# The state payload is a JS object literal: bare undefined values must
# become JSON null before json.loads can read it.
_JS_UNDEFINED_RE = re.compile(r"\bundefined\b")

# note.time values are epoch milliseconds; anything at or above this is
# treated as ms rather than seconds.
_EPOCH_MS_THRESHOLD = 1e12


class XiaoHongShuScraper(DynamicScraper):
    """Scraper for XiaoHongShu EV owner-review posts via headless Chromium."""

    SOURCE_NAME = "xiaohongshu"
    BASE_URL = "https://www.xiaohongshu.com"

    # One search keyword is tried per run (rotated daily) to keep the
    # request footprint minimal on this aggressively bot-policed source.
    _SEARCH_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "电动车评测",
        "新能源车体验",
        "智能驾驶体验",
        "BYD车主",
        "蔚来体验",
        "小鹏汽车",
        "理想汽车",
        "华为车",
    )

    # The anonymous explore feed is a general lifestyle feed, so entries
    # are kept only when the title mentions an EV brand, model, or
    # ownership/driving term. All-lowercase so latin terms match
    # case-insensitively; Chinese is unaffected by lowercasing.
    _EV_TITLE_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "电动车",
        "电车",
        "新能源",
        "智能驾驶",
        "智驾",
        "辅助驾驶",
        "自动驾驶",
        "智能座舱",
        "充电桩",
        "超充",
        "补能",
        "续航",
        "增程",
        "激光雷达",
        "noa",
        "ota",
        "提车",
        "试驾",
        "车主",
        "比亚迪",
        "byd",
        "腾势",
        "方程豹",
        "仰望",
        "蔚来",
        "nio",
        "乐道",
        "小鹏",
        "xpeng",
        "理想汽车",
        "理想l",
        "理想i",
        "理想mega",
        "问界",
        "智界",
        "享界",
        "尊界",
        "鸿蒙智行",
        "极氪",
        "zeekr",
        "零跑",
        "小米汽车",
        "su7",
        "yu7",
        "特斯拉",
        "tesla",
        "model 3",
        "model y",
        "埃安",
        "深蓝",
        "阿维塔",
        "岚图",
        "智己",
        "极狐",
        "哪吒",
    )

    # Server-rendered note cards signal a listing page has rendered.
    _LISTING_WAIT_SELECTOR: ClassVar[str] = "section.note-item"

    # The note body container; never appears on the 404/login shells, so
    # a walled or token-invalid page times out into a clean empty result.
    _NOTE_WAIT_SELECTOR: ClassVar[str] = "#noteContainer"

    # Politeness delay override: 10-15s instead of the base 5-10s.
    _DELAY_MIN_SECONDS: ClassVar[float] = 10.0
    _DELAY_MAX_SECONDS: ClassVar[float] = 15.0

    def __init__(self) -> None:
        """Set up scraper state and the per-run note-id -> xsec_token cache."""
        super().__init__()
        self._note_tokens: dict[str, str] = {}

    async def _delay(self) -> None:
        """Sleep 10-15s between requests, slower than the base class default."""
        # S311: crawl-politeness jitter, not a cryptographic use of randomness.
        delay = random.uniform(self._DELAY_MIN_SECONDS, self._DELAY_MAX_SECONDS)  # noqa: S311
        await asyncio.sleep(delay)

    async def discover_articles(self) -> list[dict[str, str]]:
        """Discover EV note URLs from one search page and the explore feed.

        Search runs first (one daily-rotated keyword; expected empty for
        anonymous sessions), then the explore feed filtered by EV title
        keywords. Entries merge JSON and DOM parses and dedupe by
        canonical URL. Returns dicts with ``url`` (tokenless canonical
        form), ``title``, and ``publish_date`` (always empty: neither
        listing carries dates; the note page supplies them).
        """
        search_entries = await self._discover_from_search()
        explore_entries = await self._discover_from_explore()
        discovered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for entry in [*search_entries, *explore_entries]:
            if entry["url"] in seen_urls:
                continue
            seen_urls.add(entry["url"])
            discovered.append(entry)
        self.logger.info(
            f"[{self.SOURCE_NAME}] discovered {len(discovered)} unique posts "
            f"({len(search_entries)} from search, {len(explore_entries)} from explore)"
        )
        return discovered

    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape one XiaoHongShu note page.

        Fetches the tokenized variant of ``url`` (cached ``xsec_token``
        re-attached; bare note URLs 404), extracts from the
        ``__INITIAL_STATE__`` note payload first, and falls back to the
        stable DOM hooks. Title-less notes use the first body line as the
        title. Returns the scraper-spec article dict, or an empty dict
        when the fetch fails (including the 404/login-wall shells) or the
        page yields no body text.
        """
        soup = await self.fetch_and_parse(
            self._build_fetch_url(url),
            wait_for=self._NOTE_WAIT_SELECTOR,
            wait_until="domcontentloaded",
        )
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] fetch failed for {url}")
            return {}
        # Capture the original page before script stripping mutates the tree.
        raw_html = str(soup)
        note_data = self._extract_note_data(soup, url)
        for element in soup.find_all(["script", "style", "noscript"]):
            element.decompose()
        title = self._extract_title(soup, note_data)
        body = self._extract_body(soup, note_data)
        if not title and body:
            title = body.split("\n", 1)[0].strip()
        if not title or not body:
            self.logger.warning(f"[{self.SOURCE_NAME}] missing title or body for {url}")
            return {}
        return {
            "source_name": self.SOURCE_NAME,
            "source_url": self._canonical_note_url(url),
            "title": title,
            "body": body,
            "publish_date": self._extract_publish_date(soup, note_data, url),
            "scrape_date": datetime.now(UTC).isoformat(),
            "language": "zh",
            "raw_html": raw_html,
        }

    async def _discover_from_search(self) -> list[dict[str, str]]:
        """Discover entries from one keyword's search results page.

        No ``wait_for`` selector: anonymous sessions get a resultless
        shell where note cards never render, and authenticated sessions
        get results server-rendered into ``__INITIAL_STATE__`` — neither
        needs (nor survives) waiting on a card selector.
        """
        keyword = self._current_search_keyword()
        url = (
            f"{self.BASE_URL}/search_result/"
            f"?keyword={quote(keyword)}&source=web_search_result_notes"
        )
        soup = await self.fetch_and_parse(url, wait_until="domcontentloaded")
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] search fetch failed for {keyword!r}")
            return []
        state = self._extract_initial_state(soup, url)
        search_state = state.get("search")
        feeds = search_state.get("feeds") if isinstance(search_state, dict) else None
        entries = self._entries_from_feed_items(feeds if isinstance(feeds, list) else [])
        entries.extend(self._entries_from_note_sections(soup))
        if not entries:
            self.logger.warning(
                f"[{self.SOURCE_NAME}] search for {keyword!r} returned no notes "
                "(search results are login-gated for anonymous sessions)"
            )
        return entries

    def _current_search_keyword(self) -> str:
        """Return today's search keyword, rotating daily through the pool."""
        index = datetime.now(UTC).timetuple().tm_yday % len(self._SEARCH_KEYWORDS)
        return self._SEARCH_KEYWORDS[index]

    async def _discover_from_explore(self) -> list[dict[str, str]]:
        """Discover EV-related entries from the anonymous explore feed."""
        soup = await self.fetch_and_parse(
            f"{self.BASE_URL}/explore",
            wait_for=self._LISTING_WAIT_SELECTOR,
            wait_until="domcontentloaded",
        )
        if soup is None:
            self.logger.warning(f"[{self.SOURCE_NAME}] explore feed fetch failed")
            return []
        state = self._extract_initial_state(soup, f"{self.BASE_URL}/explore")
        feed_state = state.get("feed")
        feeds = feed_state.get("feeds") if isinstance(feed_state, dict) else None
        entries = self._entries_from_feed_items(feeds if isinstance(feeds, list) else [])
        entries.extend(self._entries_from_note_sections(soup))
        ev_entries = [entry for entry in entries if self._is_ev_related(entry["title"])]
        self.logger.info(
            f"[{self.SOURCE_NAME}] explore feed: {len(entries)} notes, {len(ev_entries)} EV-related"
        )
        return ev_entries

    def _entries_from_feed_items(self, items: list[object]) -> list[dict[str, str]]:
        """Build listing entries from ``feed.feeds`` / ``search.feeds`` items.

        Caches each item's ``xsecToken`` for ``scrape_article``. Items
        without a hex note id or a non-empty title are skipped, as are
        non-note model types (live rooms, ads).
        """
        entries: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_type = item.get("modelType")
            if model_type is not None and model_type != "note":
                continue
            note_id = item.get("id")
            if not isinstance(note_id, str) or _NOTE_ID_RE.fullmatch(note_id) is None:
                continue
            note_card = item.get("noteCard")
            title = note_card.get("displayTitle") if isinstance(note_card, dict) else None
            if not isinstance(title, str) or not title.strip():
                continue
            token = item.get("xsecToken")
            if isinstance(token, str) and token:
                self._note_tokens[note_id] = token
            entries.append(
                {
                    "url": f"{self.BASE_URL}/explore/{note_id}",
                    "title": title.strip(),
                    "publish_date": "",
                }
            )
        return entries

    def _entries_from_note_sections(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Build listing entries from rendered ``section.note-item`` cards.

        The ``a.title`` anchor carries both the card title and the
        tokenized href; image-only cards without it are skipped. Tokens
        found in hrefs are cached for ``scrape_article``.
        """
        entries: list[dict[str, str]] = []
        for section in soup.select("section.note-item"):
            link = section.select_one("a.title[href]")
            if link is None:
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            match = _NOTE_PATH_RE.search(href)
            if match is None:
                continue
            title = link.get_text(strip=True)
            if not title:
                continue
            note_id = match.group(1)
            token_match = _XSEC_TOKEN_RE.search(href)
            if token_match:
                self._note_tokens[note_id] = token_match.group(1)
            entries.append(
                {
                    "url": f"{self.BASE_URL}/explore/{note_id}",
                    "title": title,
                    "publish_date": "",
                }
            )
        return entries

    def _is_ev_related(self, title: str) -> bool:
        """True when the title mentions an EV brand, model, or driving term."""
        lowered = title.lower()
        return any(keyword in lowered for keyword in self._EV_TITLE_KEYWORDS)

    def _build_fetch_url(self, url: str) -> str:
        """Return the tokenized fetch URL for a canonical note URL.

        Bare note URLs 404, so the cached ``xsec_token`` from discovery
        is re-attached. URLs that already carry a token, or notes with no
        cached token, pass through unchanged (the latter fetch degrades
        to the 404 shell and an empty scrape result).
        """
        if "xsec_token=" in url:
            return url
        match = _NOTE_PATH_RE.search(url)
        if match is None:
            return url
        token = self._note_tokens.get(match.group(1))
        if not token:
            return url
        return (
            f"{self.BASE_URL}/explore/{match.group(1)}"
            f"?xsec_token={quote(token, safe='=')}&xsec_source=pc_feed"
        )

    def _canonical_note_url(self, url: str) -> str:
        """Return the tokenless canonical note URL used as the dedup key."""
        match = _NOTE_PATH_RE.search(url)
        if match is None:
            return url.split("?", 1)[0]
        return f"{self.BASE_URL}/explore/{match.group(1)}"

    def _extract_initial_state(self, soup: BeautifulSoup, url: str) -> dict[str, object]:
        """Return the parsed ``window.__INITIAL_STATE__`` mapping, or {}.

        Rewrites the JS object literal's bare ``undefined`` values to
        ``null`` before parsing. A missing script, malformed payload, or
        unexpected shape degrades to an empty mapping so the DOM paths
        take over.
        """
        for script in soup.find_all("script"):
            text = script.string
            if not text or "__INITIAL_STATE__" not in text:
                continue
            match = _INITIAL_STATE_RE.search(text)
            if match is None:
                continue
            try:
                data = json.loads(_JS_UNDEFINED_RE.sub("null", match.group(1)))
            except json.JSONDecodeError:
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] malformed __INITIAL_STATE__ JSON on {url}"
                )
                return {}
            if not isinstance(data, dict):
                return {}
            return {key: value for key, value in data.items() if isinstance(key, str)}
        return {}

    def _extract_note_data(self, soup: BeautifulSoup, url: str) -> dict[str, object]:
        """Return the note mapping from ``note.noteDetailMap``, or {}.

        Looks up the URL's note id first, then ``currentNoteId``, then a
        sole map entry, so a same-page id mismatch still resolves.
        """
        note_state = self._extract_initial_state(soup, url).get("note")
        if not isinstance(note_state, dict):
            return {}
        detail_map = note_state.get("noteDetailMap")
        if not isinstance(detail_map, dict) or not detail_map:
            return {}
        match = _NOTE_PATH_RE.search(url)
        entry = detail_map.get(match.group(1)) if match else None
        if not isinstance(entry, dict):
            current_id = note_state.get("currentNoteId")
            entry = detail_map.get(current_id) if isinstance(current_id, str) else None
        if not isinstance(entry, dict) and len(detail_map) == 1:
            entry = next(iter(detail_map.values()))
        if not isinstance(entry, dict):
            return {}
        note = entry.get("note")
        if not isinstance(note, dict):
            return {}
        return {key: value for key, value in note.items() if isinstance(key, str)}

    def _extract_title(self, soup: BeautifulSoup, note_data: dict[str, object]) -> str:
        """Return the note title from the JSON payload, else ``#detail-title``."""
        title = note_data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        heading = soup.select_one("#detail-title")
        if not isinstance(heading, Tag):
            return ""
        heading_text: str = heading.get_text(strip=True)
        return heading_text

    def _extract_body(self, soup: BeautifulSoup, note_data: dict[str, object]) -> str:
        """Return the post's own text from the JSON ``desc``, else ``#detail-desc``.

        Both paths cover the note text only: author info, like counts,
        and comments live outside ``#detail-desc`` and are never
        included. Topic-tag markup inside the desc is the author's own
        text and is kept.
        """
        desc = note_data.get("desc")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
        container = soup.select_one("#detail-desc")
        if container is None:
            return ""
        note_text = container.select_one("span.note-text")
        target = note_text if note_text is not None else container
        body: str = target.get_text(" ", strip=True)
        return body

    def _extract_publish_date(
        self, soup: BeautifulSoup, note_data: dict[str, object], url: str
    ) -> str:
        """Return the publish date as ISO 8601 UTC, or empty string.

        The JSON ``time`` is epoch milliseconds. When missing or
        unusable, fall back to the visible ``span.date`` stamp.
        """
        time_value = note_data.get("time")
        if isinstance(time_value, str) and time_value.isdigit():
            time_value = int(time_value)
        if isinstance(time_value, int | float) and time_value > 0:
            seconds = time_value / 1000 if time_value >= _EPOCH_MS_THRESHOLD else time_value
            try:
                return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
            except (OverflowError, OSError, ValueError):
                self.logger.warning(
                    f"[{self.SOURCE_NAME}] unusable note time {time_value!r} on {url}"
                )
        date_element = soup.select_one("span.date")
        if date_element is None:
            return ""
        return self._parse_note_date(date_element.get_text(strip=True))

    def _parse_note_date(self, text: str) -> str:
        """Parse a visible note date (relative, absolute, or month-day) to ISO UTC."""
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
        if "今天" in text:
            return now.isoformat()
        absolute = self._parse_absolute_date(text)
        if absolute:
            return absolute
        return self._parse_month_day(text)

    def _parse_absolute_date(self, text: str) -> str:
        """Parse the first ``YYYY-MM-DD [HH:MM[:SS]]`` Beijing timestamp to ISO UTC."""
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

    def _parse_month_day(self, text: str) -> str:
        """Parse a yearless ``MM-DD`` Beijing date stamp to ISO 8601 UTC.

        The current Beijing year is assumed; stamps that would land in
        the future (a December note read in January) roll back one year.
        """
        match = _MONTH_DAY_RE.search(text)
        if match is None:
            return ""
        now_cn = datetime.now(_CHINA_TZ)
        try:
            stamped = datetime(
                now_cn.year, int(match.group(1)), int(match.group(2)), tzinfo=_CHINA_TZ
            )
        except ValueError:
            return ""
        if stamped - now_cn > timedelta(days=1):
            stamped = stamped.replace(year=now_cn.year - 1)
        return stamped.astimezone(UTC).isoformat()

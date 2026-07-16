# Scraper Specification

## Class Hierarchy

```
BaseScraper (backend/scrapers/base.py)
├── StaticScraper (backend/scrapers/static.py)
│   ├── GasgooScraper (backend/scrapers/sources/gasgoo.py)
│   ├── CnEVPostScraper (backend/scrapers/sources/cnevpost.py)
│   ├── BaiduNewsScraper (backend/scrapers/sources/baidu_news.py)
│   └── [Phase 2] ThirtySixKrScraper (backend/scrapers/sources/_36kr.py)
└── DynamicScraper (backend/scrapers/dynamic.py)
    ├── AutohomeScraper (backend/scrapers/sources/autohome.py)
    └── [Phase 2] DongchediScraper, XiaoHongShuScraper
```

## BaseScraper Responsibilities

- Rate limiting: `random.uniform(5, 10)` second delay between requests
- UA rotation: pool of 10-15 real browser UA strings, stored in config
- Retry: exponential backoff, max 3 retries per request
- Request logging: every request logs URL, status code, response size, timestamp
- Error handling: catch and log all exceptions, never crash the runner
- Health metrics: track requestsMade, errorCount, errors, durationSeconds for `scraper_health` collection

## StaticScraper

Extends BaseScraper. Uses httpx (async) for HTTP requests and BeautifulSoup4 for HTML parsing. For static HTML sites and RSS feeds.

## DynamicScraper

Extends BaseScraper. Uses Playwright (async) for browser automation. For JS-rendered pages, infinite scroll, login walls.

## Source Scraper Interface

Every source scraper must implement these two methods:

```python
class SourceScraper:
    SOURCE_NAME: str                  # e.g., "gasgoo"
    BASE_URL: str                     # e.g., "https://autonews.gasgoo.com"

    async def discover_articles(self) -> list[dict]:
        """
        Find new article URLs to scrape from listing/index pages.
        Returns list of:
        {
            "url": str,
            "title": str,           # if available from listing page
            "publish_date": str,    # ISO 8601, if available
        }
        """

    async def scrape_article(self, url: str) -> dict:
        """
        Scrape a single article page and extract content.
        Returns:
        {
            "source_name": str,
            "source_url": str,
            "title": str,
            "body": str,            # full article text
            "publish_date": str,    # ISO 8601
            "scrape_date": str,     # ISO 8601, set by this method
            "language": str,        # "zh" or "en"
            "raw_html": str,        # original HTML for debugging
        }
        """
```

## Runner (backend/scrapers/runner.py)

The runner is the cron entrypoint. Flow:

1. Load source scrapers in sequence (never parallel)
2. For each source, call `discover_articles()`
3. Deduplicate against existing articles in Firestore (by `sourceUrl`)
4. For each new URL, call `scrape_article()`
5. Write raw article data to `articles` collection with `processed: false`
6. Write health metrics to `scraper_health` collection
7. Exit. LLM processing runs as a separate step.

## Per-Source Notes

### Tier 1 (Phase 1)

| Source | Scraper Type | Difficulty | Notes |
|---|---|---|---|
| Gasgoo (gasgoo.com) | StaticScraper (RSS) | Easy | English + Chinese, has RSS feed, simple article structure |
| CnEVPost (cnevpost.com) | StaticScraper (RSS) | Easy | English, has RSS feed, clean HTML |
| Baidu News (news.baidu.com) | StaticScraper | Easy | Search results page, paginated, stable structure |
| Autohome (autohome.com.cn) | DynamicScraper | Hard | JS-rendered, anti-bot. Focus on: new model pages, spec sheets, tech news section |

Build order: Gasgoo first (easiest, has RSS, English), then CnEVPost, then Baidu News, then Autohome.

### Tier 2 (Phase 2)

| Source | Scraper Type | Difficulty | Notes |
|---|---|---|---|
| Dongchedi (dongchedi.com) | DynamicScraper | Hard | ByteDance property, aggressive anti-bot. Focus on: model listings, feature comparisons, editorial reviews |
| XiaoHongShu (xiaohongshu.com) | DynamicScraper | Hard | Social platform, infinite scroll, login walls. Focus on: EV owner review posts |
| 36kr (36kr.com) | StaticScraper | Easy | Tech/startup news, clean article pages. File: `_36kr.py` (underscore prefix, module names cannot start with a number) |

### Tier 3 (Stretch)

Bilibili teardown channels (Whisper transcription), Weibo auto KOLs, MIIT announcements.

## Fallback Plan (if a source blocks)

1. Adjust timing, rotate UA pool, change scrape schedule
2. Add ScraperAPI proxy for that source only ($29/mo)
3. Last resort: paid API wrapper for that source only

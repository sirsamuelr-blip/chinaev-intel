---
name: new-scraper
description: Checklist for building a new source scraper. Use when the task is to add a new scraper source (e.g., "build the dongchedi scraper", "add 36kr source").
---

# New Scraper Checklist

Follow every step in order. Do not skip steps.

## 1. Read the spec
Read @docs/scraper-spec.md for the full interface contract and per-source notes.

## 2. Determine scraper type
- Static site (plain HTML, RSS) → extend `StaticScraper` from `backend/scrapers/static.py`
- JS-rendered site → extend `DynamicScraper` from `backend/scrapers/dynamic.py`

## 3. Create the source file
- File: `backend/scrapers/sources/<source_name>.py`
- Class: `<SourceName>Scraper` (PascalCase)
- Must define `SOURCE_NAME: str` and `BASE_URL: str`
- Must implement `async def discover_articles(self) -> list[dict]`
- Must implement `async def scrape_article(self, url: str) -> dict`
- Return shapes must match scraper-spec.md exactly

## 4. Register in runner
- Import in `backend/scrapers/runner.py`, add to sequential execution list

## 5. Create test fixtures
- Save 2-3 sample HTML pages in `backend/tests/fixtures/<source_name>/`

## 6. Write tests
- File: `backend/tests/scrapers/test_<source_name>.py`
- Test discover_articles and scrape_article against fixtures
- Test error handling: 404, timeout, malformed HTML
- Mock all HTTP requests

## 7. Check dependency license
- If any new package is needed, verify license is MIT/Apache-2.0/BSD/ISC

## 8. Verify
```bash
cd backend
ruff check scrapers/sources/<source_name>.py
ruff format scrapers/sources/<source_name>.py
mypy scrapers/sources/<source_name>.py
pytest tests/scrapers/test_<source_name>.py -v
pytest  # full suite for regressions
```

## 9. Commit
```
feat: add <source_name> scraper with <discovery_method> discovery
```

Update CHANGELOG.md under [Unreleased].

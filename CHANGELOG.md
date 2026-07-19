# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Scraper runner cron entrypoint: sequential execution of all Tier 1 scrapers, Firestore URL dedup before scraping, per-source health metrics with success/partial/failure status, per-source crash isolation, and optional LLM pipeline trigger
- Autohome source scraper: Playwright-rendered discovery across the news, tech, and EV listing pages with cross-section URL dedup, article extraction from the Next.js `__NEXT_DATA__` payload with stable-DOM fallbacks, and Beijing-to-UTC date normalization
- DynamicScraper: headless Chromium via async Playwright with per-request page isolation, networkidle waits, optional wait-for-selector hook, and error-tolerant browser shutdown
- Baidu News source scraper: keyword search discovery across five Chinese EV/software terms with cross-keyword URL dedup, Chinese relative/absolute listing-date normalization to UTC, and structure-agnostic article extraction with title/body/date fallback chains
- LLM extraction pipeline: Claude Sonnet translation and structured extraction of unprocessed articles, JSON validation with brace-boundary recovery, exponential-backoff retries, per-article error isolation
- CnEVPost source scraper: RSS discovery via the WordPress feed, article extraction of title, body, and UTC publish date, with embedded subscription/related-post chrome stripped from the body
- Gasgoo source scraper: RSS discovery across the Market & Industry, EV, and ICV category feeds with URL dedup, plus article extraction of title, body, and UTC publish date
- Async Firestore helpers for `articles` and `scraper_health`: save article, dedup check by sourceUrl, unprocessed-article queue, post-processing updates, error recording, health metrics
- StaticScraper: managed async httpx client, HTML parsing via BeautifulSoup, and RSS/Atom feed parsing via feedparser
- BaseScraper abstract base class: randomized rate limiting, User-Agent rotation, retry with exponential backoff, request logging, and health metrics
- Project scaffold: backend structure, reference docs, CI pipeline
- CLAUDE.md progressive disclosure architecture (root + backend + docs/)
- Playbook enforcement: pre-commit hooks, scoped rules, skills, CI security gates
- PR template, issue templates, CODEOWNERS
- ADRs: progressive disclosure (#001), component library selection (#002)
- Security: CSRF rules, audit logging rules, dependency license checking
- Docs: phase 3 frontend spec, revenue-critical paths, MCP registry, rollback plan, tech debt register

### Fixed
- Update ruff (v0.15.21) and mypy (v1.14.0) pre-commit hook versions; record Context7 3.2.4 in MCP registry and defer GitHub MCP to gh CLI
- Add dotenv to mypy ignore-missing-imports overrides

# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- Fixed date parsing to handle empty values without log spam

### Changed
feat/brand-aliases
- Scoped Phase 2 processing to unprocessed articles only using phase2Processed flag with 100-article cap
main
- Switched extraction pipeline to Anthropic Batch API for 50% cost reduction
- Removed full English translation from extraction pipeline to reduce per-article API cost ~70%

### Added
feat/brand-aliases
- Expanded brand_aliases.json with 15+ Chinese EV sub-brands and added non-automotive brand filtering
- Per-run brand-resolution cache and a Sonnet call cap (with brace-salvage JSON parsing) in entity promotion to bound API cost
feat/brand-aliases
- Expanded brand_aliases.json with 15+ Chinese EV sub-brands and added non-automotive brand filtering
- Per-run brand-resolution cache and a Sonnet call cap (with brace-salvage JSON parsing) in entity promotion to bound API cost feat/brand-aliases
- Expanded brand_aliases.json with 15+ Chinese EV sub-brands and added non-automotive brand filtering
- Per-run brand-resolution cache and a Sonnet call cap (with brace-salvage JSON parsing) in entity promotion to bound API cost main
 main
- Added Haiku pre-filtering to skip low-relevance articles before full Sonnet extraction
- Added prompt caching on extraction prompt to reduce input token costs
- XiaoHongShu scraper for consumer EV reviews and owner experiences (Playwright)
- Dongchedi scraper for ByteDance automotive platform coverage (Playwright)
- Full Phase 2 pipeline integration: entity promotion, dedup, signal detection, and novelty scoring wired into runner (opt-in via `--phase2`)
- Shared Claude API retry helper extracted to `processing/utils.py`
- Competitive comparison engine: feature matrix, timeline, brand comparison, and price-feature analysis
- Novelty scoring module for articles and signals with configurable lookback windows
- 36kr scraper for Chinese tech/startup news with auto sector coverage
- Signal detection module: rule-based triggers for 6 signal types with LLM narrative generation
- Signal narrative prompt template for competitive intelligence brief entries
- Firestore CRUD methods for signals collection
- Cross-source article deduplication using multi-signal similarity scoring
- Firestore CRUD for dedup field updates and recent article queries
- Entity promotion module: resolves and upserts brands, vehicles, features from processed articles
- Brand alias dictionary with 40+ Chinese EV brands and suppliers
- Firestore CRUD methods for brands, vehicles, and features collections
- ADRs: signal detection approach (#003), deduplication strategy (#004), entity resolution strategy (#005)
- Configurable per-source article cap (MAX_ARTICLES_PER_SOURCE, default 25) in the scraper runner; prevents Autohome's 180-article discovery from creating hour-long runs
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
- Autohome article pages no longer timeout on persistent analytics connections; DynamicScraper.fetch_page now accepts a wait_until parameter (default networkidle), and AutohomeScraper uses domcontentloaded with CSS selector waits
- Update ruff (v0.15.21) and mypy (v1.14.0) pre-commit hook versions; record Context7 3.2.4 in MCP registry and defer GitHub MCP to gh CLI
- Add dotenv to mypy ignore-missing-imports overrides

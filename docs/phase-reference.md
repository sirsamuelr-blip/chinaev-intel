# Build Phases

Do not build features from a later phase unless the task prompt explicitly says to.

## Phase 1: Data Pipeline (Weeks 1-2)

Scrapers, Firestore ingestion, LLM extraction, health logging.

Scope:
- Firebase project + Firestore collections + indexes
- VPS setup with Python, Playwright, cron
- BaseScraper, StaticScraper, DynamicScraper classes
- Tier 1 source scrapers: Gasgoo, CnEVPost, Baidu News, Autohome
- LLM processing pipeline (translate + extract via Sonnet)
- Scraper runner (cron entrypoint)
- Health logging to `scraper_health` collection
- Automotive glossary for translation QA
- Cron schedule: scrape every 6 hours, LLM processing after each scrape

Output: Database filling with structured Chinese EV intelligence.

## Phase 2: Intelligence Layer (Weeks 3-4)

Signal detection, competitive comparison, Tier 2 sources, deduplication.

Scope:
- Signal detection logic (new launches, trickle-down, AI integrations, OTA, partnerships, hardware)
- Competitive comparison engine (feature matrix, timeline, price-to-feature)
- Tier 2 scrapers: Dongchedi, XiaoHongShu, 36kr
- Deduplication (same story from multiple sources)
- Novelty scoring (new vs recycled news)

Output: Structured signals with competitive implications.

## Phase 3: Admin Dashboard (Weeks 5-6)

Your operations cockpit. Build before the subscriber dashboard.

Scope:
- React + Tailwind frontend initialization (Vite)
- Firebase Auth (admin-only access)
- Pipeline health view (source status, error logs, manual re-run, trend charts)
- Content review queue (article feed, approve/flag/discard, inline edit)
- Signal management (status toggles, drag-to-reorder, merge duplicates)
- Digest builder (generate draft via Opus, rich text editor, preview, send test, schedule)
- Subscriber management (list, add/remove, gift trials, revenue dashboard)
- System analytics (articles ingested, signals generated, extraction accuracy, API costs)

Output: Fully operational command center.

## Phase 4: Report Generation (Weeks 7-8)

Automated weekly intelligence brief.

Scope:
- Brief template design (Top 3 Signals, Feature Watch, AI & Software, What to Steal, Data Table)
- Report generation with Claude Opus
- Wire digest builder to generation endpoint
- Email pipeline (Resend): markdown to HTML, subscriber list, send scheduling (Tuesday AM ET)
- Substack cross-post mechanism

Output: Weekly brief auto-generating, editable, and sending.

## Phase 5: Subscriber Dashboard (Weeks 9-10)

Self-serve paid access to the intelligence database.

Scope:
- Firebase Auth (email/password)
- Stripe paywall (monthly/annual)
- Feature database search + filter (by brand, category, date, segment)
- Brand profile pages (timeline, software stack, recent signals)
- Feature comparison tool (side-by-side, exportable to PDF/PPTX)
- Saved searches + email alerts
- Past digest archive

Output: Paid SaaS product live.

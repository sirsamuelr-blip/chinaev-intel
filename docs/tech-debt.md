# Technical Debt Register

Track known shortcuts, deferred improvements, and quality gaps.

| Date | Description | Severity | Remediation Plan |
|---|---|---|---|
| 2026-07-20 | Baidu News blocks all datacenter IPs with CAPTCHA regardless of region. Confirmed blocked from both US (local dev) and Singapore (DigitalOcean VPS). Scraper logic is correct; this is an IP reputation issue requiring residential proxy or proxy API service. | Medium | Add ScraperAPI ($29/mo) for Baidu News only when revenue justifies the cost. Three other sources are producing data. Not blocking Phase 2. |
| 2026-07-20 | Autohome article pages intermittently timed out on `networkidle` wait — persistent analytics connections prevented idle state. Fixed by adding a `wait_until` parameter to `DynamicScraper.fetch_page`; Autohome now passes `domcontentloaded` and relies on `wait_for` selectors for render completion. | Resolved | Fixed in feat: add wait_until parameter to DynamicScraper |
| 2026-07-20 | VPS deployment requires `python -m playwright install chromium` after Python setup. Not automated in any script yet. | Low | Add to deployment script or setup checklist before first cron run. |
| 2026-07-20 | Firestore composite index for `articles` collection (`processed` + `scrapeDate`) must be created manually. Not automated. | Low | Add to deployment checklist. Consider `firebase.json` index definitions for automated deploy. |
| 2026-07-20 | Runner scrapes all discovered articles per source with no per-source cap. Autohome discovers 180 per run — at ~15-90s each, a full run takes over an hour. | Resolved | Fixed: configurable MAX_ARTICLES_PER_SOURCE (default 25) added to runner |
| 2026-07-20 | Duplicated _call_claude retry helper in entities.py (copied from pipeline.py to avoid circular import) | Resolved | Fixed: shared call_claude extracted to backend/processing/utils.py; pipeline, entities, and signals all import it |
| 2026-07-20 | get_recent_processed_articles returns snake_case but dedup functions expect camelCase — conversion deferred to runner wiring | Resolved | Fixed: keys_to_camel (now public in db/firestore.py) bridge applied in runner.run_phase2_processing before the Phase 2 steps |
| 2026-07-22 | Full article translation removed from extraction prompt to reduce API costs. bodyEn field not populated. Add on-demand translation in Phase 5 when subscriber dashboard needs it. | Medium | Implement translate-on-click in Phase 5 subscriber dashboard. |

Severity: Critical (blocks release or security risk), High (fix within 2 weeks), Medium (fix before next phase), Low (nice to have).

Review at the start of each new phase.

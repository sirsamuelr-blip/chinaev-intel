# Technical Debt Register

Track known shortcuts, deferred improvements, and quality gaps.

| Date | Description | Severity | Remediation Plan |
|---|---|---|---|
| 2026-07-20 | Baidu News returns CAPTCHAs from US IP — all keyword searches get 302 to captcha page. Scraper logic is correct; this is an IP reputation issue. Will likely resolve from HK/SG VPS. | Medium | Test from VPS first. If still blocked, add ScraperAPI proxy for Baidu only ($29/mo). |
| 2026-07-20 | Autohome article pages intermittently timed out on `networkidle` wait — persistent analytics connections prevented idle state. Fixed by adding a `wait_until` parameter to `DynamicScraper.fetch_page`; Autohome now passes `domcontentloaded` and relies on `wait_for` selectors for render completion. | Resolved | Fixed in feat: add wait_until parameter to DynamicScraper |
| 2026-07-20 | VPS deployment requires `python -m playwright install chromium` after Python setup. Not automated in any script yet. | Low | Add to deployment script or setup checklist before first cron run. |
| 2026-07-20 | Firestore composite index for `articles` collection (`processed` + `scrapeDate`) must be created manually. Not automated. | Low | Add to deployment checklist. Consider `firebase.json` index definitions for automated deploy. |
| 2026-07-20 | Runner scrapes all discovered articles per source with no per-source cap. Autohome discovers 180 per run — at ~15-90s each, a full run takes over an hour. | Resolved | Fixed: configurable MAX_ARTICLES_PER_SOURCE (default 25) added to runner |

Severity: Critical (blocks release or security risk), High (fix within 2 weeks), Medium (fix before next phase), Low (nice to have).

Review at the start of each new phase.

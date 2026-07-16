# CLAUDE.md

## Project

ChinaEV Intel: automated competitive intelligence pipeline that monitors Chinese EV industry sources, extracts structured data about software features and competitive moves via the Claude API, stores results in Firestore, and delivers weekly intelligence briefs to paying subscribers through email and a React dashboard.

## Tech Stack

- Python 3.12, httpx, BeautifulSoup4, Playwright, anthropic SDK
- Firebase: Firestore + Auth + Storage (firebase-admin)
- React 18 (Vite) + Tailwind CSS 3
- Resend (email), Stripe (payments)
- pytest, ruff
- Vercel (frontend), Railway (workers), HK/SG VPS (scrapers)

## Repository Structure

- `backend/` — Python: scrapers, LLM processing, Firestore operations
- `backend/scrapers/` — BaseScraper → StaticScraper / DynamicScraper → source scrapers
- `backend/scrapers/sources/` — One file per source (gasgoo.py, cnevpost.py, etc.)
- `backend/processing/` — Claude API extraction pipeline
- `backend/db/` — Firestore client and CRUD helpers
- `backend/config/` — Settings, environment variables, translation glossary
- `backend/tests/` — pytest, mirrors source structure, fixtures in tests/fixtures/
- `frontend/` — React app (admin at /admin/*, subscriber at /dashboard/*). Initialized in Phase 3.
- `docs/` — Reference docs: schemas, scraper spec, pipeline spec, phase definitions

## Commands

```
cd backend && pytest                  # run all tests
cd backend && pytest tests/scrapers/  # scraper tests only
cd backend && pytest -x              # stop on first failure
cd backend && ruff check .           # lint
cd backend && ruff format .          # format
```

## Reference Docs

Read these when working on the relevant area. Do not guess at schemas or interfaces.

- @docs/firestore-schema.md — All Firestore collections, fields, types, indexes
- @docs/scraper-spec.md — Scraper interface, request patterns, per-source notes
- @docs/llm-pipeline.md — Extraction prompt, I/O shapes, model selection, glossary
- @docs/phase-reference.md — Build phases and scope boundaries

## Environment Variables

Required now: `ANTHROPIC_API_KEY`, `FIREBASE_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`

Later phases: `RESEND_API_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID`, `VITE_FIREBASE_STORAGE_BUCKET`, `VITE_FIREBASE_MESSAGING_SENDER_ID`, `VITE_FIREBASE_APP_ID`

All loaded in `backend/config/settings.py` via python-dotenv. Never hardcode secrets.

## Rules

1. **No paid API wrappers.** All scrapers custom-built with httpx + BS4 or Playwright. No parse.bot, Apify, ScraperAPI, or similar. ScraperAPI as proxy only if a source blocks, developer decision only.
2. **No alternative tech suggestions.** The stack is fixed. Do not suggest substitutions.
3. **Always write tests.** Every new module, function, or class gets corresponding tests. No PR without tests.
4. **Check existing patterns first.** Before writing new helpers, check `backend/db/firestore.py`, `backend/config/settings.py`, and `backend/scrapers/base.py` for existing utilities.
5. **Scraper class hierarchy is mandatory.** Source scrapers extend `StaticScraper` or `DynamicScraper`. Never extend `BaseScraper` directly. Never bypass inherited rate limiting or retry logic.
6. **Sonnet for article processing, Opus for digest generation.** Do not switch models.
7. **JSON only from Claude API.** All extraction prompts request JSON-only responses. Validate every response. Handle malformed JSON gracefully.
8. **Firestore denormalization is intentional.** Denormalize fields needed for queries. No cross-collection joins in application code.
9. **Never store secrets in code.** All secrets in `.env`, loaded via `backend/config/settings.py`.
10. **One source scraper per file** in `backend/scrapers/sources/`.
11. **Async everywhere for I/O.** All scrapers, database operations, and API calls must be async.
12. **Log, do not crash.** A single article or scraper failure must never crash the runner. Log the error, record it in `scraper_health`, continue.
13. **Feature branches for all work.** Never commit directly to `main`.
14. **Imperative commit messages.** "Add gasgoo scraper" not "Added gasgoo scraper".
15. **Do not build features from later phases** unless the task prompt explicitly says to. Read @docs/phase-reference.md for phase boundaries.

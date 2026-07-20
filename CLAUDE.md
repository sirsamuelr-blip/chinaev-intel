# CLAUDE.md

## Project

ChinaEV Intel: automated competitive intelligence pipeline that monitors Chinese EV industry sources, extracts structured data about software features and competitive moves via the Claude API, stores results in Firestore, and delivers weekly intelligence briefs to paying subscribers through email and a React dashboard.

## Tech Stack

- Python 3.12, httpx, BeautifulSoup4, Playwright, anthropic SDK
- Firebase: Firestore + Auth + Storage (firebase-admin)
- React 18 (Vite) + Tailwind CSS 3
- Resend (email), Stripe (payments)
- pytest, pytest-cov, ruff, mypy (strict), gitleaks, semgrep, pip-audit
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
- `docs/` — Reference docs, ADRs, tech debt register, phase specs
- `.claude/rules/` — Scoped rules loaded by file glob pattern
- `.claude/skills/` — Repeatable workflow checklists loaded on demand

## Commands

```
cd backend && pytest                          # run all tests
cd backend && pytest --cov=. --cov-report=term # tests with coverage
cd backend && pytest tests/scrapers/          # scraper tests only
cd backend && pytest -x                       # stop on first failure
cd backend && ruff check .                    # lint
cd backend && ruff format .                   # format
cd backend && mypy .                          # type check (strict)
```

Pre-commit hooks run automatically: ruff, mypy, gitleaks, conventional commit validation.

## Reference Docs

Read these when working on the relevant area. Do not guess at schemas or interfaces.

- @docs/firestore-schema.md — All Firestore collections, fields, types, indexes
- @docs/scraper-spec.md — Scraper interface, request patterns, per-source notes
- @docs/llm-pipeline.md — Extraction prompt, I/O shapes, model selection, glossary
- @docs/phase-reference.md — Build phases and scope boundaries
- @docs/phase3-frontend-spec.md — Frontend tooling, design tokens, accessibility requirements
- @docs/revenue-critical-paths.md — Paths that must have test coverage before shipping

## Environment Variables

Required now: `ANTHROPIC_API_KEY`, `FIREBASE_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`

Later phases: `RESEND_API_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `VITE_FIREBASE_*`

All loaded in `backend/config/settings.py` via python-dotenv. Never hardcode secrets.

## Workflow Rules

1. **Plan Mode for multi-file changes.** If a task touches 3 or more files, use Plan Mode before writing code. Outline which files change and why.
2. **Conventional Commits.** Every commit: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Enforced by pre-commit hook.
3. **Verification before commit.** Run `ruff check .`, `ruff format .`, `mypy .`, and `pytest` before every commit. All must pass.
4. **Show evidence.** When asked if tests pass, show the actual output.
5. **Context management.** Use `/compact` at ~50% context or after completing a milestone. Use `/clear` when switching to an unrelated topic.
6. **Git checkpoint before risky changes.** Before any refactor touching 5+ files or any change to auth/payments/security: commit or stash current work first.
7. **Use subagents for research.** For research-heavy tasks (reading docs, exploring APIs), use the Task tool to keep the main context clean.

## Hard Rules

1. **No paid API wrappers.** All scrapers custom-built with httpx + BS4 or Playwright.
2. **No alternative tech suggestions.** The stack is fixed.
3. **Always write tests.** Every new module, function, or class gets tests. No commit without tests.
4. **Check existing patterns first.** Check `backend/db/firestore.py`, `backend/config/settings.py`, and `backend/scrapers/base.py` before creating new helpers.
5. **Scraper class hierarchy is mandatory.** Source scrapers extend `StaticScraper` or `DynamicScraper`. Never `BaseScraper` directly.
6. **Sonnet for article processing, Opus for digest generation.** Do not switch models.
7. **JSON only from Claude API.** Validate every response. Handle malformed JSON gracefully.
8. **Firestore denormalization is intentional.** No cross-collection joins in application code.
9. **Never store secrets in code.** All secrets in `.env`, loaded via `backend/config/settings.py`.
10. **One source scraper per file** in `backend/scrapers/sources/`.
11. **Async everywhere for I/O.** All scrapers, database operations, and API calls must be async.
12. **Log, do not crash.** Single failures never crash the runner. Log and continue.
13. **Feature branches for all work.** Never commit directly to `main`.
14. **Do not build features from later phases** unless the task prompt explicitly says to.
15. **Update CHANGELOG.md on every `feat:` or `fix:` commit.** Add a line under `[Unreleased]`.
16. **Maximum 5 MCP servers.** See docs/mcp-servers.md. Do not install additional servers without explicit approval.
17. **No third-party Claude Code skills or plugins** without explicit approval. Unknown skills consume tokens and may conflict with project conventions.
18. Backend/ is a flat layout with no top-level __init__.py
## Security Rules

AI-generated code introduces OWASP vulnerabilities in 45% of cases. Apply extra scrutiny.

18. **Never ship `allow read, write: if true` in Firestore rules.** Always validate ownership. Use `hasOnly()`.
19. **Never store tokens in localStorage.** Use httpOnly cookies.
20. **No auto-accept on auth, payments, Firestore security rules, or crypto code.** Human review before merge.
21. **Rate limit all public endpoints.**
22. **Validate all inputs server-side.**
23. **Verify dependency licenses before adding packages.** MIT/Apache-2.0/BSD/ISC only. No GPL/AGPL/SSPL.

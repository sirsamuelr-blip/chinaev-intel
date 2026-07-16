# Backend Conventions

## Python Standards

- Type hints on all function signatures. Use `from __future__ import annotations` at top of every file.
- mypy strict mode compliance. No `Any` types unless explicitly justified with a comment.
- Docstrings on all public functions and classes.
- `async`/`await` for all I/O: scrapers, Firestore, Claude API.
- `logging` module only. Never `print()`. Use `logger = logging.getLogger(__name__)`.
- f-strings for formatting. No `.format()`, no `%`.
- `pathlib.Path` for file paths. Not `os.path`.
- No bare except clauses. Catch specific exceptions.
- Maximum function length: 50 lines. Break up longer functions.
- No wildcard imports. No commented-out code in commits.

## Naming

- Files: `snake_case.py`
- Variables, functions: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private methods: `_leading_underscore`
- Test files: `test_<module_name>.py`

## Import Order

1. Standard library
2. Third-party packages
3. Local imports

Blank line between each group. ruff handles sorting via isort rules.

## Scraper Architecture

Class hierarchy: `BaseScraper` → `StaticScraper` (httpx + BS4) / `DynamicScraper` (Playwright) → source scrapers in `scrapers/sources/`.

- `base.py` — Rate limiting, UA rotation, retry with exponential backoff, logging, health metrics
- `static.py` — httpx + BeautifulSoup for static/RSS sites
- `dynamic.py` — Playwright for JS-rendered sites
- `sources/*.py` — One file per source, extends Static or Dynamic
- `runner.py` — Cron entrypoint: runs all scrapers sequentially, writes to Firestore, logs health

Request patterns enforced in BaseScraper (inherited by all):
- 5-10s randomized delay, UA rotation, off-peak China hours (2-6 AM CST)
- Sequential execution, never parallel, max 3 retries with exponential backoff
- Every request logged: URL, status code, response size, timestamp

Full interface spec: @docs/scraper-spec.md

## LLM Processing

- Sonnet (`claude-sonnet-4-6`) for all article processing
- Opus (`claude-opus-4-6`) for weekly digest generation only (Phase 4)
- All prompts request JSON-only, no preamble, no markdown fences
- On malformed JSON: log error, set `processingError` on article doc, skip
- On API failure: retry with exponential backoff, max 3

Full prompt template, I/O shapes, and glossary: @docs/llm-pipeline.md

## Testing

- Framework: pytest + pytest-asyncio + pytest-cov
- Test files in `tests/`, mirroring source structure
- HTML fixtures in `tests/fixtures/*.html`, API fixtures in `tests/fixtures/*.json`
- Mock all external I/O. No real HTTP requests or API calls in tests.
- Firestore: use emulator or mock the client.
- `conftest.py` holds shared fixtures

## Firestore Security

- Never `allow read, write: if true`. Not even for testing.
- Always check `request.auth.uid == resource.data.userId` for user-owned data.
- Use `hasOnly()` to prevent field injection.
- Validate data shape in security rules, not just auth state.
- Firestore and Storage rules are separate deploys. Update both.
- Test with Firebase Emulator Suite before deploying.

Full schema: @docs/firestore-schema.md

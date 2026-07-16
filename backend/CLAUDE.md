# Backend Conventions

## Python Standards

- Type hints on all function signatures. Use `from __future__ import annotations` at top of every file.
- Docstrings on all public functions and classes.
- `async`/`await` for all I/O: scrapers, Firestore, Claude API.
- `logging` module only. Never `print()`. Use `logger = logging.getLogger(__name__)`.
- f-strings for formatting. No `.format()`, no `%`.
- `pathlib.Path` for file paths. Not `os.path`.
- No bare except clauses. Catch specific exceptions.
- Maximum function length: 50 lines. Break up longer functions.
- No wildcard imports.
- No commented-out code in commits.

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

Blank line between each group.

## Scraper Architecture

Class hierarchy: `BaseScraper` → `StaticScraper` (httpx + BS4) / `DynamicScraper` (Playwright) → source scrapers in `scrapers/sources/`.

- `base.py` — Rate limiting, UA rotation, retry with exponential backoff, logging, health metrics
- `static.py` — httpx + BeautifulSoup for static/RSS sites
- `dynamic.py` — Playwright for JS-rendered sites
- `sources/*.py` — One file per source, extends Static or Dynamic
- `runner.py` — Cron entrypoint: runs all scrapers sequentially, writes raw articles to Firestore, logs health

Request patterns enforced in BaseScraper and inherited by all scrapers:
- 5-10s randomized delay between requests (`random.uniform(5, 10)`)
- UA rotation from pool of 10-15 real browser strings
- Off-peak China hours (2-6 AM CST / UTC+8)
- Sequential execution, never parallel
- Max 3 retries with exponential backoff
- Every request logged: URL, status code, response size, timestamp

Full interface spec: @docs/scraper-spec.md

## LLM Processing

- Sonnet (`claude-sonnet-4-6`) for all article processing (translate + extract)
- Opus (`claude-opus-4-6`) for weekly digest generation only (Phase 4)
- All prompts request JSON-only, no preamble, no markdown fences
- Validate every response against expected schema
- On malformed JSON: log error, set `processingError` on article doc, skip. Do not set `processed = true`.
- On API failure: retry with exponential backoff, max 3

Full prompt template, I/O shapes, and glossary: @docs/llm-pipeline.md

## Testing

- Framework: pytest + pytest-asyncio
- Test files in `tests/`, mirroring source structure
- HTML fixtures in `tests/fixtures/*.html` (saved sample pages from each source)
- Claude API response fixtures in `tests/fixtures/*.json`
- Mock all external I/O. No real HTTP requests or API calls in tests.
- Firestore: use emulator or mock the client.
- `conftest.py` holds shared fixtures: mock Firestore client, mock Claude client, sample HTML

## Firestore

Client init and all CRUD helpers live in `db/firestore.py`.
Full schema with all collections, fields, and types: @docs/firestore-schema.md

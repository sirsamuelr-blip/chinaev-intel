# Contributing to ChinaEV Intel

## Development Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your API keys
3. Install Python 3.12
4. Install backend dependencies: `cd backend && pip install -r requirements.txt`
5. Install pre-commit hooks: `pre-commit install && pre-commit install --hook-type commit-msg`
6. Run tests: `cd backend && pytest`
7. Run lint: `cd backend && ruff check .`
8. Run type check: `cd backend && mypy .`

## Workflow

1. Create a feature branch from `main`: `git checkout -b feat/your-feature`
2. Make changes in small, focused commits
3. Pre-commit hooks run automatically: ruff, mypy, gitleaks, commit message validation
4. Every commit must use Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
5. Push and open a PR against `main` using the PR template
6. CI must pass: lint, format, type check, tests, security scans
7. Update CHANGELOG.md under `[Unreleased]` for any `feat:` or `fix:` commit

## Code Standards

- Python: type hints on all functions (mypy strict), async for I/O, logging (no print), max 50-line functions
- Tests: every new module gets tests, mock all external I/O, fixtures in `backend/tests/fixtures/`
- Security: no secrets in code, no `allow read, write: if true`, no localStorage for tokens, verify dependency licenses

## Architecture Decisions

Recorded as ADRs in `docs/adr/`. Add one for any decision affecting structure, data models, or tooling.

## Technical Debt

Tracked in `docs/tech-debt.md`. Add entries when shortcuts are taken. Review at each phase start.
